from __future__ import annotations

import queue
import re
import threading
import time
import tkinter as tk
import os
from tkinter import scrolledtext, ttk

from .config import (
    ConfigError, config_file, load, load_successful_connection, remember_successful_connection,
)
from .kt_runtime import KTRuntime, KTRuntimeError
from .ssh_tunnel import SSHTunnel, SSHTunnelError, validate_ssh_profile
from .provisioning import (
    DEFAULT_ORIGIN,
    PROVISIONING_NOT_CONFIGURED_MESSAGE,
    ProvisioningError,
    ProvisioningUnavailable,
    check_provisioning_configuration,
    discover_default_origin,
    normalize_origin,
    provision_and_save,
    verify_public_endpoint,
    wait_for_public_endpoint,
)
from .startup import StartupError, is_startup_enabled, set_startup_enabled
from .supervisor import Supervisor, SupervisorError
from .tray import TrayController
from .instance import GUIInstance, InstanceError


TITLE = "独立 MCP 地址"
# Keep the helper text short: the detailed implementation notes belong in the
# README, not in the small launcher window.
NOTICE = "启动本机 MCP，并在验证成功后显示地址。"
REQUEST_LABEL = "启动并获取地址"
STATUS_PREVIEW_LIMIT = 120
RUNTIME_CONFIG_MISSING_MESSAGE = (
    "独立 KT runtime 配置缺失：请使用便携版启动入口；"
    "自定义配置需要 kt_python 和 workspace_path，kt_home 可使用独立默认目录。"
)


class ClientGUI:
    """Run blocking work off-thread; all Tk operations stay on the UI thread."""

    def __init__(
        self,
        root: tk.Tk | None = None,
        *,
        config_path: str | None = None,
        startup_mode: bool = False,
    ) -> None:
        self.root = root or tk.Tk()
        self.root.title(TITLE)
        # Let Tk fit the content height so wrapped status text stays visible
        # without reserving an empty block below the address fields.
        self.root.resizable(False, False)
        self.root.minsize(640, 180)
        self.config_path = config_path
        self.startup_mode = bool(startup_mode)
        # Restore only connection input; never assume a cached endpoint is live.
        saved_origin, saved_key = self._saved_form_settings()
        self.origin_var = tk.StringVar(value=saved_origin)
        self.provision_key_var = tk.StringVar(value=saved_key)
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="状态：等待请求")
        self.elapsed_var = tk.StringVar(value="准备就绪")
        self._worker: threading.Thread | None = None
        self._shutdown_worker: threading.Thread | None = None
        self._shutdown_poll_id = None
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._started_at = 0.0
        self._stage_at = 0.0
        self._stage = ""
        self._poll_id = None
        self._closing = False
        self._shutdown_requested = False
        self._service_lock = threading.Lock()
        self._runtime: KTRuntime | None = None
        self._tunnel: SSHTunnel | None = None
        self._supervisor: Supervisor | None = None
        self._tray_controller = TrayController(
            self.root,
            on_show=self._show_window,
            on_exit=self._quit_app,
        )
        self._tray_available = False
        self._startup_enabled = self._read_startup_state()
        self.startup_var = tk.BooleanVar(value=self._startup_enabled)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _saved_form_settings(self) -> tuple[str, str]:
        try:
            origin, key = load_successful_connection(self.config_path)
            if not origin or normalize_origin(origin) == normalize_origin(DEFAULT_ORIGIN):
                return "", ""
            return normalize_origin(origin), key
        except (ConfigError, ProvisioningError, OSError, ValueError, TypeError):
            # Corrupt/foreign-user DPAPI data must not prevent the GUI opening.
            return "", ""

    @staticmethod
    def _read_startup_state() -> bool:
        try:
            return is_startup_enabled()
        except StartupError:
            return False

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=False)
        outer.columnconfigure(1, weight=1)
        outer.columnconfigure(2, weight=0)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, minsize=210)
        heading = ttk.Frame(header)
        heading.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        ttk.Label(heading, text=TITLE, font=("TkDefaultFont", 13, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        ttk.Label(heading, text=NOTICE, wraplength=380).grid(
            row=1, column=0, sticky="w"
        )
        self.status_panel = ttk.Frame(header)
        self.status_panel.grid(row=0, column=1, sticky="ne")
        self.startup_checkbutton = ttk.Checkbutton(
            self.status_panel,
            text="开机自启（静默启动）",
            variable=self.startup_var,
            command=self._toggle_startup,
        )
        self.startup_checkbutton.grid(row=0, column=0, sticky="e")
        self._status_display_var = tk.StringVar(value=self.status_var.get())
        self.status_label = ttk.Label(
            self.status_panel,
            textvariable=self._status_display_var,
            wraplength=210,
            anchor="e",
            justify="right",
        )
        self.status_label.grid(row=1, column=0, sticky="e", pady=(3, 1))
        self.status_label.bind("<Button-1>", self._show_status_details)
        self.status_label.bind("<Return>", self._show_status_details)
        self.status_var.trace_add("write", self._refresh_status_label)
        self.elapsed_label = ttk.Label(
            self.status_panel,
            textvariable=self.elapsed_var,
            wraplength=210,
            anchor="e",
            justify="right",
        )
        self.elapsed_label.grid(row=2, column=0, sticky="e")

        ttk.Label(outer, text="公网 origin").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.origin_entry = ttk.Entry(outer, textvariable=self.origin_var, width=42)
        self.origin_entry.grid(row=1, column=1, sticky="ew", pady=3)
        self.paste_button = ttk.Button(outer, text="粘贴", command=self._paste_origin)
        self.paste_button.grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=3)
        ttk.Label(outer, text="验证 key").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        self.provision_key_entry = ttk.Entry(
            outer, textvariable=self.provision_key_var, width=42, show="*"
        )
        self.provision_key_entry.grid(row=2, column=1, sticky="ew", pady=3)
        self.request_button = ttk.Button(outer, text=REQUEST_LABEL, command=self.request_endpoint)
        self.request_button.grid(row=2, column=2, sticky="e", padx=(8, 0), pady=3)
        ttk.Label(outer, text="MCP 地址").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(outer, textvariable=self.output_var, state="readonly", width=42).grid(
            row=3, column=1, sticky="ew", pady=3
        )
        self.copy_button = ttk.Button(outer, text="复制", command=self._copy_endpoint, state="disabled")
        self.copy_button.grid(row=3, column=2, sticky="ew", padx=(8, 0), pady=3)
        # Keep the indeterminate bar as an internal progress signal for the
        # worker/tests, but do not show a busy animation in the compact UI.
        self.progress_bar = ttk.Progressbar(outer, mode="indeterminate")

    def _refresh_status_label(self, *_args: object) -> None:
        full_text = self.status_var.get()
        text = " ".join(full_text.split())
        show_details = len(text) > STATUS_PREVIEW_LIMIT or text != full_text
        if len(text) > STATUS_PREVIEW_LIMIT:
            text = text[:STATUS_PREVIEW_LIMIT] + "…"
        if show_details:
            text += "（点击查看详情）"
        self._status_display_var.set(text)
        self.status_label.configure(cursor="hand2" if show_details else "", takefocus=show_details)

    def _show_status_details(self, _event: object = None) -> None:
        text = self.status_var.get()
        if len(text) > STATUS_PREVIEW_LIMIT or " ".join(text.split()) != text:
            self._open_status_details(text)

    def _open_status_details(self, text: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("状态详情")
        dialog.transient(self.root)
        dialog.geometry("620x340")
        details = scrolledtext.ScrolledText(dialog, wrap="word", font="TkDefaultFont", padx=8, pady=8)
        details.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        details.insert("1.0", text)
        details.configure(state="disabled")
        ttk.Button(dialog, text="关闭", command=dialog.destroy).pack(pady=8)

    def _paste_origin(self) -> None:
        if self._closing or self._worker is not None:
            return
        try:
            value = self.root.clipboard_get().strip()
        except tk.TclError:
            self.status_var.set("状态：剪贴板中没有可粘贴的文本。")
            return
        if value:
            self.origin_var.set(value)

    def _copy_endpoint(self) -> None:
        endpoint = self.output_var.get().strip()
        if self._closing or not endpoint:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(endpoint)
        except tk.TclError:
            self.status_var.set("状态：无法写入剪贴板，请手动复制 MCP 地址。")
            return
        self.elapsed_var.set("MCP 地址已复制")

    def _toggle_startup(self) -> None:
        """Persist the startup checkbox in this user's HKCU Run key."""

        enabled = bool(self.startup_var.get())
        try:
            set_startup_enabled(enabled, self.config_path)
        except StartupError as exc:
            # Restore the previous state when Windows rejects the operation.
            self.startup_var.set(self._startup_enabled)
            self.status_var.set("状态：" + str(exc)[:1000])
            return
        self._startup_enabled = enabled
        text = "已启用开机自动启动" if enabled else "已关闭开机自动启动"
        self.status_var.set("状态：" + text)

    def _ensure_tray(self) -> bool:
        """Start the native notification-area icon once, if available."""

        if self._tray_available:
            return True
        self._tray_available = self._tray_controller.start()
        return self._tray_available

    def _show_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _stop_local_services(self) -> None:
        """Stop only this client's independent KT and SSH processes."""

        with self._service_lock:
            supervisor, tunnel, runtime = self._supervisor, self._tunnel, self._runtime
        if supervisor is not None:
            stopped = supervisor.stop()
            if (
                stopped.runtime.get("cli_status") != "stopped"
                or stopped.runtime.get("local_ready")
                or stopped.runtime.get("process", {}).get("running")
                or stopped.tunnel.get("process", {}).get("running")
                or stopped.detail
            ):
                raise SupervisorError("本地 MCP 或 SSH 隧道尚未确认停止")
            return

        # Reopened windows adopt the same independent config/PID records,
        # without constructing a second supervisor or probing before stop.
        if runtime is None or tunnel is None:
            filename = config_file(self.config_path)
            if filename.is_file():
                profile = load(filename)
                if runtime is None:
                    candidate = KTRuntime(profile, filename.parent / "runtime", config_root=filename.parent)
                    # A never-started installation has no endpoint directory
                    # and needs no KT stop command. Existing state is checked.
                    if candidate.connection_path.parent.exists():
                        runtime = candidate
                tunnel = tunnel or SSHTunnel(profile, filename.parent / "runtime")
        errors: list[Exception] = []
        if tunnel is not None:
            try:
                stopped_tunnel = tunnel.stop()
                if stopped_tunnel.process.running or stopped_tunnel.detail:
                    raise SSHTunnelError("SSH 隧道尚未确认停止")
            except Exception as exc:
                errors.append(exc)
        if runtime is not None:
            try:
                stopped_runtime = runtime.stop()
                if (
                    stopped_runtime.cli_status != "stopped"
                    or stopped_runtime.local_ready
                    or stopped_runtime.process.running
                ):
                    raise KTRuntimeError("本地 MCP 尚未确认停止")
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise SupervisorError("本地 MCP 或 SSH 隧道停止失败") from errors[0]

    def _shutdown_worker_entry(self, startup_worker: threading.Thread | None) -> None:
        try:
            # No service may be created after final cleanup. Tk stays alive
            # and responsive while an in-flight startup finishes cancelling.
            if startup_worker is not None:
                startup_worker.join()
            self._stop_local_services()
        except Exception as exc:
            self._events.put(("shutdown", None, exc))
        else:
            self._events.put(("shutdown", None, None))

    def _poll_shutdown(self) -> None:
        self._shutdown_poll_id = None
        while True:
            try:
                kind, _value, error = self._events.get_nowait()
            except queue.Empty:
                break
            if kind != "shutdown":
                continue
            self._shutdown_worker = None
            self._worker = None
            if error is not None:
                self._closing = False
                self.status_var.set("状态：停止未完成，请重试托盘菜单中的“退出并停止本地 MCP”。")
                self._tray_controller.set_tooltip("KohakuTerrarium MCP - 停止失败")
                self._show_window()
                return
            self._tray_controller.stop()
            self.root.destroy()
            return
        self._shutdown_poll_id = self.root.after(75, self._poll_shutdown)

    def _quit_app(self) -> None:
        """Return immediately to the tray menu; stop services off-thread."""

        if self._closing:
            return
        self._closing = True
        self._shutdown_requested = True
        self.status_var.set("状态：正在停止本地 MCP 并退出…")
        self._tray_controller.set_tooltip("KohakuTerrarium MCP - 正在退出")
        self.output_var.set("")
        for widget in (self.request_button, self.origin_entry, self.provision_key_entry,
                       self.paste_button, self.copy_button, self.startup_checkbutton):
            widget.configure(state="disabled")
        self.progress_bar.stop()
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        self._shutdown_worker = threading.Thread(
            target=self._shutdown_worker_entry,
            args=(self._worker,),
            name="kt-mcp-gui-shutdown",
            daemon=False,
        )
        self._shutdown_worker.start()
        self._shutdown_poll_id = self.root.after(75, self._poll_shutdown)

    def _has_saved_origin(self) -> bool:
        """Check logon-resume eligibility without populating the form."""

        try:
            profile = load(config_file(self.config_path))
            origin = normalize_origin(str(getattr(profile, "public_origin", "") or ""))
            return origin != normalize_origin(DEFAULT_ORIGIN)
        except Exception:
            return False

    def _report_progress(self, text: str) -> None:
        # Never call root.after/StringVar/any Tk function from the worker.
        self._events.put(("progress", text, time.monotonic()))

    def _poll_events(self) -> None:
        self._poll_id = None
        if self._closing:
            return
        while True:
            try:
                kind, value, extra = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self._stage, self._stage_at = value, extra
                self.status_var.set("状态：" + value)
            elif kind == "saved_connection":
                self.origin_var.set(value)
                self.provision_key_var.set(extra)
            elif kind == "result":
                self._finish_request(value, extra)
        if self._worker is not None:
            now = time.monotonic()
            self.elapsed_var.set(f"已用 {now - self._started_at:.1f} 秒")
            self._poll_id = self.root.after(100, self._poll_events)

    def _pause_monitoring(self) -> Supervisor | None:
        """Retire the previous owner before adopting this config again.

        Never join a monitor while holding the GUI service lock. The shutdown
        worker joins startup first, so it cannot race a replacement here.
        """
        previous = getattr(self, "_supervisor", None)
        if previous is not None:
            previous.stop_monitoring()
            with self._service_lock:
                self._supervisor = None
        return previous

    def _wait_for_public(self, endpoint_url: str, profile, *, remember: bool = True) -> None:
        self._report_progress("等待 SSH 隧道就绪并验证公网 MCP（最多 45 秒）")
        wait_for_public_endpoint(
            endpoint_url,
            profile,
            cancelled=lambda: bool(getattr(self, "_shutdown_requested", False)),
            on_retry=lambda attempt, reason: self._report_progress(
                f"等待公网就绪，第 {attempt} 次重试：{reason}"
            ),
            verify=verify_public_endpoint,
        )
        if getattr(self, "_shutdown_requested", False):
            raise ProvisioningError("客户端正在退出；未保存连接结果")
        if remember:
            remember_successful_connection(profile, self.config_path)
            # Queue UI input changes; workers must never touch Tk variables.
            self._events.put(("saved_connection", profile.public_origin, profile.provision_token))

    def _runtime_and_provision(self, origin: str, provision_key: str = "") -> str:
        self._report_progress("[1/4] 检查本机配置与服务器注册凭据")
        filename = config_file(self.config_path)
        if not filename.is_file():
            raise ProvisioningError(RUNTIME_CONFIG_MISSING_MESSAGE)
        # Snapshot legacy input before KT's URL refresh can save its profile.
        # A corrupt optional history file must not block manually fixing input.
        try:
            load_successful_connection(self.config_path)
        except (ConfigError, OSError):
            pass
        profile = load(self.config_path)
        missing = [name for name, value in (
            ("kt_python", profile.kt_python), ("workspace_path", profile.workspace_path)
        ) if not str(value or "").strip()]
        if missing:
            raise ProvisioningError(RUNTIME_CONFIG_MISSING_MESSAGE + " 缺少：" + ", ".join(missing))
        profile.public_origin = normalize_origin(origin)
        # An omitted key keeps a previously loaded DPAPI token.  This matters
        # for legacy/programmatic callers that invoke the worker without the
        # new optional form field; an explicitly supplied key still replaces it.
        saved_token = getattr(profile, "provision_token", "")
        requested_token = str(provision_key or "").strip() or saved_token
        profile.provision_token = requested_token
        # If the operator did not provide a separate API override, the
        # explicitly entered origin is the intended /provision target.
        previous_provision_url = os.environ.get("KT_MCP_PROVISION_URL")
        had_provision_env = previous_provision_url is not None
        if not previous_provision_url or not previous_provision_url.strip():
            os.environ["KT_MCP_PROVISION_URL"] = profile.public_origin + "/provision"
        # Missing credentials and unsafe origin/override combinations must fail
        # now, not after an expensive KT startup.
        # This is local validation only; it does not claim the API is reachable.
        previous_owner = None
        try:
            check_provisioning_configuration(profile.public_origin, profile=profile)
            # KT url() persists its profile. Do not store an unvalidated new
            # registration key before the registration service accepts it.
            profile.provision_token = saved_token
            previous_owner = self._pause_monitoring()
            config_root = filename.parent
            runtime = KTRuntime(profile, config_root / "runtime", config_root=config_root)
            service_lock = getattr(self, "_service_lock", None)
            if service_lock is None:
                self._runtime = runtime
            else:
                with service_lock:
                    self._runtime = runtime
            if getattr(self, "_shutdown_requested", False):
                raise KTRuntimeError("客户端正在退出；未启动独立 KT")
            self._report_progress("[2/4] 初始化独立 KT 与工作区")
            runtime.setup()
            if getattr(self, "_shutdown_requested", False):
                raise KTRuntimeError("客户端正在退出；未启动独立 KT")
            self._report_progress("[3/4] 启动本地 KT（注册前不等待公网路由）")
            start_status = runtime.start(wait_for_public=False)
            cli_status = str(getattr(start_status, "cli_status", "")).casefold()
            if cli_status not in {"ready", "degraded"} or getattr(start_status, "local_ready", False) is not True:
                detail = getattr(start_status, "detail", "") or "本地 KT 尚未就绪"
                raise KTRuntimeError(f"KT start 未 ready：{detail}")
            if getattr(self, "_shutdown_requested", False):
                raise KTRuntimeError("客户端正在退出；未提交公网注册")
            self._report_progress("[4/4] 本地 KT 已就绪，读取真实地址并提交服务器注册")
            endpoint_url = runtime.url()
            profile.provision_token = requested_token
            provision_and_save(profile.public_origin, endpoint_url, profile=profile, config_path=self.config_path)
            missing_ssh = [
                name for name in ("ssh_host", "ssh_user", "ssh_identity_file", "ssh_known_hosts")
                if not str(getattr(profile, name, "") or "").strip()
            ]
            if missing_ssh:
                raise SSHTunnelError(
                    "provisioning 已成功，但 SSH 凭据未配置：缺少 " + ", ".join(missing_ssh)
                    + "。服务器未返回完整的自动接入配置；请升级服务端后重试，旧验证 key 无需更换，也不要复制管理员私钥。"
                )
            validate_ssh_profile(profile)
            tunnel = SSHTunnel(profile, config_root / "runtime")
            if service_lock is None:
                self._tunnel = tunnel
            else:
                with service_lock:
                    self._tunnel = tunnel
            if getattr(self, "_shutdown_requested", False):
                raise SSHTunnelError("客户端正在退出；未启动 SSH 隧道")
            tunnel_status = tunnel.start()
            process = getattr(tunnel_status, "process", None)
            if not (
                getattr(tunnel_status, "configured", False)
                and process is not None
                and getattr(process, "running", False)
                and getattr(process, "identity_ok", False)
            ):
                raise SSHTunnelError(getattr(tunnel_status, "detail", "SSH 隧道未 ready"))
            if getattr(self, "_shutdown_requested", False):
                raise SSHTunnelError("客户端正在退出；未完成公网验证")
            # One lifecycle owner adopts the exact pair used for enrollment.
            # Monitoring starts even if the first public check is still pending.
            supervisor = Supervisor(
                profile, config_root / "runtime", runtime=runtime, tunnel=tunnel,
            )
            if service_lock is None:
                self._supervisor = supervisor
            else:
                with service_lock:
                    self._supervisor = supervisor
            supervisor.monitor()
            self._wait_for_public(endpoint_url, profile)
            return endpoint_url
        finally:
            if had_provision_env:
                os.environ["KT_MCP_PROVISION_URL"] = str(previous_provision_url)
            else:
                os.environ.pop("KT_MCP_PROVISION_URL", None)
            # A failed retry must not strand a previously healthy pair without
            # a reconnect owner. Transfer ownership only after replacement.
            if (
                previous_owner is not None
                and self._supervisor is None
                and not getattr(self, "_shutdown_requested", False)
            ):
                with self._service_lock:
                    self._supervisor = previous_owner
                    self._runtime = previous_owner.runtime
                    self._tunnel = previous_owner.tunnel
                try:
                    previous_owner.monitor()
                except Exception:
                    # Preserve the registration/startup error; shutdown still
                    # retains the old owner and can stop its exact pair.
                    self._report_progress("原连接监测恢复失败，请重试连接")

    def _resume_saved_services(self) -> str:
        """Restart an already enrolled device without re-registering it."""

        self._report_progress("[1/3] 恢复本机 MCP 和 SSH 隧道")
        filename = config_file(self.config_path)
        if not filename.is_file():
            raise SupervisorError(RUNTIME_CONFIG_MISSING_MESSAGE)
        profile = load(filename)
        self._pause_monitoring()
        supervisor = Supervisor(profile, filename.parent / "runtime")
        service_lock = getattr(self, "_service_lock", None)
        if service_lock is None:
            self._supervisor = supervisor
            self._runtime = supervisor.runtime
            self._tunnel = supervisor.tunnel
        else:
            with service_lock:
                self._supervisor = supervisor
                self._runtime = supervisor.runtime
                self._tunnel = supervisor.tunnel
        if getattr(self, "_shutdown_requested", False):
            raise SupervisorError("客户端正在退出；未恢复独立 MCP")
        status = supervisor.start(wait_for_public=False)
        if status.state not in {"running", "degraded"}:
            raise SupervisorError(status.detail or "本机 MCP 或 SSH 隧道未 ready")
        if getattr(self, "_shutdown_requested", False):
            raise SupervisorError("客户端正在退出；未完成公网验证")
        self._report_progress("[2/3] 读取本机 endpoint")
        endpoint_url = supervisor.runtime.url()
        self._report_progress("[3/3] 验证公网 MCP")
        self._wait_for_public(endpoint_url, profile, remember=False)
        return endpoint_url

    def _finish_request(self, endpoint: str | None, error: BaseException | None) -> None:
        self.output_var.set("")
        self.copy_button.configure(state="disabled")
        if error is None and endpoint:
            self.output_var.set(endpoint)
            self.copy_button.configure(state="normal")
            self.status_var.set("状态：公网 MCP 已完成 initialize、tools/list 与无害 tree 验证。")
        elif isinstance(error, (ProvisioningError, ConfigError, KTRuntimeError, SupervisorError, SSHTunnelError)):
            text = re.sub(r"https?://[^\s\"'<>]+/mcp/[^\s\"'<>]+", "<已隐藏地址>", str(error))
            text = re.sub(r"[A-Za-z0-9_-]{43,128}", "<已隐藏凭据>", text)
            self.status_var.set("状态：" + text[:1200])
        else:
            self.status_var.set("状态：独立 runtime/provisioning 失败，请检查配置后重试")
        elapsed = max(0.0, time.monotonic() - self._started_at)
        self.elapsed_var.set(f"本次耗时 {elapsed:.2f} 秒")
        self.progress_bar.stop()
        self.origin_entry.configure(state="normal")
        self.provision_key_entry.configure(state="normal")
        self.paste_button.configure(state="normal")
        self.request_button.configure(state="normal")
        self._worker = None

    def _worker_entry(self, origin: str, provision_key: str = "") -> None:
        try:
            endpoint = (
                self._runtime_and_provision(origin, provision_key)
                if provision_key
                else self._runtime_and_provision(origin)
            )
        except BaseException as exc:
            self._events.put(("result", None, exc))
        else:
            self._events.put(("result", endpoint, None))

    def _resume_worker_entry(self) -> None:
        try:
            endpoint = self._resume_saved_services()
        except BaseException as exc:
            self._events.put(("result", None, exc))
        else:
            self._events.put(("result", endpoint, None))

    def resume_saved_services(self) -> None:
        """Start the background logon-resume worker without showing the UI."""

        if self._worker is not None or self._closing or self._shutdown_requested:
            return
        self._started_at = self._stage_at = time.monotonic()
        self._stage = "[1/3] 恢复本机 MCP 和 SSH 隧道"
        self.output_var.set("")
        self.copy_button.configure(state="disabled")
        self.paste_button.configure(state="disabled")
        self.request_button.configure(state="disabled")
        self.origin_entry.configure(state="disabled")
        self.provision_key_entry.configure(state="disabled")
        self.status_var.set("状态：" + self._stage)
        self.elapsed_var.set("已用 0.0 秒")
        self.progress_bar.start(12)
        worker = threading.Thread(
            target=self._resume_worker_entry,
            name="kt-mcp-gui-startup",
            daemon=True,
        )
        self._worker = worker
        worker.start()
        self._poll_id = self.root.after(75, self._poll_events)

    def request_endpoint(self) -> None:
        # Keep the worker reference until its result is consumed. A second click
        # cannot race a completed worker whose result is still queued.
        if self._worker is not None or self._closing or self._shutdown_requested:
            return
        self._started_at = self._stage_at = time.monotonic()
        self._stage = "[1/4] 检查本机配置与服务器注册凭据"
        self.output_var.set("")
        self.copy_button.configure(state="disabled")
        self.paste_button.configure(state="disabled")
        self.request_button.configure(state="disabled")
        self.origin_entry.configure(state="disabled")
        self.provision_key_entry.configure(state="disabled")
        self.status_var.set("状态：" + self._stage)
        self.elapsed_var.set("已用 0.0 秒")
        self.progress_bar.start(12)
        provision_key = self.provision_key_var.get().strip()
        worker_args = (self.origin_var.get().strip(), provision_key) if provision_key else (self.origin_var.get().strip(),)
        worker = threading.Thread(
            target=self._worker_entry,
            args=worker_args,
            name="kt-mcp-gui-provision",
            daemon=True,
        )
        self._worker = worker
        worker.start()
        self._poll_id = self.root.after(75, self._poll_events)

    def _on_close(self) -> None:
        # Closing the window keeps the local MCP and SSH tunnel alive.  The
        # tray menu is the explicit, separate action that stops both services.
        if self._closing:
            return
        if not self._ensure_tray():
            # A development environment may not have pywin32 or a Windows
            # notification area; keep a visible taskbar button rather than
            # making the process unreachable.
            try:
                self.root.iconify()
            except Exception:
                pass
            return
        try:
            self.root.withdraw()
        except Exception:
            pass

    provision = request_endpoint

    def run(self) -> None:
        self.root.mainloop()


def run_gui(
    config_path: str | None = None,
    *,
    startup_mode: bool = False,
    instance: GUIInstance | None = None,
    instance_guard: GUIInstance | None = None,
) -> None:
    if instance is not None and instance_guard is not None and instance is not instance_guard:
        raise ValueError("只允许传入一个 GUI single-instance guard")
    instance = instance_guard or instance
    gui = ClientGUI(config_path=config_path, startup_mode=startup_mode)
    gui._ensure_tray()
    if instance is not None:
        def poll_activation() -> None:
            if gui._closing:
                return
            try:
                if instance.consume_activation():
                    gui._show_window()
            except InstanceError:
                return
            try:
                gui.root.after(100, poll_activation)
            except Exception:
                return

        gui.root.after(100, poll_activation)
    if startup_mode:
        # Logon startup is always silent. A missing saved origin simply
        # leaves the client idle in the tray until the user opens the window.
        try:
            gui.root.withdraw()
        except Exception:
            pass
        if gui._has_saved_origin():
            gui.root.after(100, gui.resume_saved_services)
    gui.run()


__all__ = [
    "ClientGUI", "DEFAULT_ORIGIN", "NOTICE", "PROVISIONING_NOT_CONFIGURED_MESSAGE",
    "REQUEST_LABEL", "RUNTIME_CONFIG_MISSING_MESSAGE", "TITLE", "discover_default_origin", "run_gui",
]
