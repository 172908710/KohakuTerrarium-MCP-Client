from __future__ import annotations

import queue
import re
import threading
import time
import tkinter as tk
import os
from tkinter import ttk

from .config import ConfigError, config_file, load
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
)


TITLE = "独立 MCP 地址"
# Keep the helper text short: the detailed implementation notes belong in the
# README, not in the small launcher window.
NOTICE = "启动本机 MCP，并在验证成功后显示地址。"
REQUEST_LABEL = "启动并获取地址"
RUNTIME_CONFIG_MISSING_MESSAGE = (
    "独立 KT runtime 配置缺失：请使用便携版启动入口；"
    "自定义配置需要 kt_python 和 workspace_path，kt_home 可使用独立默认目录。"
)


class ClientGUI:
    """Run blocking work off-thread; all Tk operations stay on the UI thread."""

    def __init__(self, root: tk.Tk | None = None, *, config_path: str | None = None) -> None:
        self.root = root or tk.Tk()
        self.root.title(TITLE)
        # The launcher is intentionally fixed-size.  This avoids a large empty
        # area below the status text while keeping long URLs horizontally
        # scrollable inside their entry widgets.
        self.root.resizable(False, False)
        self.root.minsize(560, 210)
        self.root.geometry("640x230")
        self.config_path = config_path
        # Reuse only this device's locally protected profile.  The sanitized
        # placeholder origin is never shown as a real deployment value.
        saved_origin, saved_key = self._saved_profile_values()
        self.origin_var = tk.StringVar(value=saved_origin)
        self.provision_key_var = tk.StringVar(value=saved_key)
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="状态：等待请求")
        self.elapsed_var = tk.StringVar(value="准备就绪")
        self._worker: threading.Thread | None = None
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._started_at = 0.0
        self._stage_at = 0.0
        self._stage = ""
        self._poll_id = None
        self._closing = False
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=False)
        outer.columnconfigure(1, weight=1)
        outer.columnconfigure(2, weight=0)
        ttk.Label(outer, text=TITLE, font=("TkDefaultFont", 13, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        ttk.Label(outer, text=NOTICE, wraplength=600).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        ttk.Label(outer, text="公网 origin").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        self.origin_entry = ttk.Entry(outer, textvariable=self.origin_var, width=42)
        self.origin_entry.grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(outer, text="验证 key").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        self.provision_key_entry = ttk.Entry(
            outer, textvariable=self.provision_key_var, width=42, show="*"
        )
        self.provision_key_entry.grid(row=3, column=1, sticky="ew", pady=3)
        self.request_button = ttk.Button(outer, text=REQUEST_LABEL, command=self.request_endpoint)
        self.request_button.grid(row=3, column=2, sticky="e", padx=(8, 0), pady=3)
        ttk.Label(outer, text="MCP 地址").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(outer, textvariable=self.output_var, state="readonly", width=42).grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=3
        )
        ttk.Label(outer, textvariable=self.status_var, wraplength=560).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(5, 2)
        )
        # Keep the indeterminate bar as an internal progress signal for the
        # worker/tests, but do not show a busy animation in the compact UI.
        self.progress_bar = ttk.Progressbar(outer, mode="indeterminate")
        ttk.Label(outer, textvariable=self.elapsed_var, wraplength=560).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(0, 0)
        )

    def _saved_profile_values(self) -> tuple[str, str]:
        """Load this device's origin and DPAPI-protected key for the form."""

        try:
            profile = load(config_file(self.config_path))
            origin = normalize_origin(str(getattr(profile, "public_origin", "") or ""))
            if origin == normalize_origin(DEFAULT_ORIGIN):
                origin = ""
            token = str(getattr(profile, "provision_token", "") or "")
            return origin, token
        except Exception:
            return "", ""

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
            elif kind == "result":
                self._finish_request(value, extra)
        if self._worker is not None:
            now = time.monotonic()
            self.elapsed_var.set(f"已用 {now - self._started_at:.1f} 秒")
            self._poll_id = self.root.after(100, self._poll_events)

    def _runtime_and_provision(self, origin: str, provision_key: str = "") -> str:
        self._report_progress("[1/4] 检查本机配置与服务器注册凭据")
        filename = config_file(self.config_path)
        if not filename.is_file():
            raise ProvisioningError(RUNTIME_CONFIG_MISSING_MESSAGE)
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
        if str(provision_key or "").strip():
            profile.provision_token = str(provision_key).strip()
        # If the operator did not provide a separate API override, the
        # explicitly entered origin is the intended /provision target.
        previous_provision_url = os.environ.get("KT_MCP_PROVISION_URL")
        had_provision_env = previous_provision_url is not None
        if not previous_provision_url or not previous_provision_url.strip():
            os.environ["KT_MCP_PROVISION_URL"] = profile.public_origin + "/provision"
        # Missing credentials and unsafe origin/override combinations must fail
        # now, not after an expensive KT startup.
        # This is local validation only; it does not claim the API is reachable.
        try:
            check_provisioning_configuration(profile.public_origin, profile=profile)
            config_root = filename.parent
            runtime = KTRuntime(profile, config_root / "runtime", config_root=config_root)
            self._report_progress("[2/4] 初始化独立 KT 与工作区")
            runtime.setup()
            self._report_progress("[3/4] 启动本地 KT（注册前不等待公网路由）")
            start_status = runtime.start(wait_for_public=False)
            cli_status = str(getattr(start_status, "cli_status", "")).casefold()
            if cli_status not in {"ready", "degraded"} or getattr(start_status, "local_ready", False) is not True:
                detail = getattr(start_status, "detail", "") or "本地 KT 尚未就绪"
                raise KTRuntimeError(f"KT start 未 ready：{detail}")
            self._report_progress("[4/4] 本地 KT 已就绪，读取真实地址并提交服务器注册")
            endpoint_url = runtime.url()
            provision_and_save(profile.public_origin, endpoint_url, profile=profile, config_path=self.config_path)
            missing_ssh = [
                name for name in ("ssh_host", "ssh_user", "ssh_identity_file", "ssh_known_hosts")
                if not str(getattr(profile, name, "") or "").strip()
            ]
            if missing_ssh:
                raise SSHTunnelError("provisioning 已成功，但 SSH 凭据未配置：缺少 " + ", ".join(missing_ssh))
            validate_ssh_profile(profile)
            tunnel = SSHTunnel(profile, config_root / "runtime")
            tunnel_status = tunnel.start()
            process = getattr(tunnel_status, "process", None)
            if not (
                getattr(tunnel_status, "configured", False)
                and process is not None
                and getattr(process, "running", False)
                and getattr(process, "identity_ok", False)
            ):
                raise SSHTunnelError(getattr(tunnel_status, "detail", "SSH 隧道未 ready"))
            verify_public_endpoint(endpoint_url, profile)
            return endpoint_url
        finally:
            if had_provision_env:
                os.environ["KT_MCP_PROVISION_URL"] = str(previous_provision_url)
            else:
                os.environ.pop("KT_MCP_PROVISION_URL", None)

    def _finish_request(self, endpoint: str | None, error: BaseException | None) -> None:
        self.output_var.set("")
        if error is None and endpoint:
            self.output_var.set(endpoint)
            self.status_var.set("状态：公网 MCP 已完成 initialize、tools/list 与无害 tree 验证。")
        elif isinstance(error, (ProvisioningError, ConfigError, KTRuntimeError)):
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

    def request_endpoint(self) -> None:
        # Keep the worker reference until its result is consumed. A second click
        # cannot race a completed worker whose result is still queued.
        if self._worker is not None or self._closing:
            return
        self._started_at = self._stage_at = time.monotonic()
        self._stage = "[1/4] 检查本机配置与服务器注册凭据"
        self.output_var.set("")
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
        self._closing = True
        if self._poll_id is not None:
            self.root.after_cancel(self._poll_id)
        # Closing a window is not authorization to stop an existing KT endpoint.
        # An in-flight worker only uses its queue and never touches destroyed Tk.
        self.root.destroy()

    provision = request_endpoint

    def run(self) -> None:
        self.root.mainloop()


def run_gui(config_path: str | None = None) -> None:
    ClientGUI(config_path=config_path).run()


__all__ = [
    "ClientGUI", "DEFAULT_ORIGIN", "NOTICE", "PROVISIONING_NOT_CONFIGURED_MESSAGE",
    "REQUEST_LABEL", "RUNTIME_CONFIG_MISSING_MESSAGE", "TITLE", "discover_default_origin", "run_gui",
]
