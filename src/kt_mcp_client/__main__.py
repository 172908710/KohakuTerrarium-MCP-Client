from __future__ import annotations

import argparse
import json
import sys

from .config import ConfigError, config_file, load
from .gui import run_gui
from .instance import GUIInstance, InstanceError
from .kt_runtime import KTRuntime, KTRuntimeError
from .provisioning import (
    ProvisioningError,
    check_provisioning_configuration,
    provision_and_save,
    verify_public_endpoint,
)
from .ssh_tunnel import SSHTunnel, SSHTunnelError, validate_ssh_profile
from .supervisor import Supervisor, SupervisorError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KT MCP 客户端（独立版）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="显示状态")
    group.add_argument("--start", action="store_true", help="启动 KT 和可用的 SSH 隧道")
    group.add_argument("--provision", action="store_true", help="启动独立 KT 并注册其实际 endpoint URL")
    group.add_argument("--stop", action="store_true", help="停止 SSH 和 KT")
    group.add_argument("--url", action="store_true", help="读取并输出独立 KT 的实际 MCP 地址（敏感信息）")
    parser.add_argument(
        "--startup",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--config", help="配置目录或 profile.json 路径")
    return parser


def _provision(args: argparse.Namespace, profile, config_root) -> str:
    """Run local KT enrollment, then bring up the returned SSH route.

    The provisioning response is the authority for ``remote_forward_port``.
    No SSH object is started until ``provision_and_save`` has validated that
    response and applied the port to this same in-memory profile.
    """

    check_provisioning_configuration(profile.public_origin, profile=profile)
    runtime = KTRuntime(profile, config_root / "runtime", config_root=config_root)
    runtime.setup()
    start_status = runtime.start(wait_for_public=False)
    # A new external endpoint normally starts local-only: the public route and
    # SSH reverse tunnel are provisioned after the local URL is known. Permit
    # that documented degraded state, but never continue when KT itself did
    # not report a local listener.
    cli_status = str(getattr(start_status, "cli_status", "")).casefold()
    if cli_status not in {"ready", "degraded"} or getattr(start_status, "local_ready", False) is not True:
        detail = getattr(start_status, "detail", "") or "KT start 未 ready"
        raise KTRuntimeError(f"KT start 未 ready：{detail}")

    endpoint_url = runtime.url()
    # Use the resolved config root when --config was omitted. This keeps the
    # installation id/profile and the tunnel state in one independent root.
    provision_and_save(
        profile.public_origin,
        endpoint_url,
        profile=profile,
        config=args.config or config_root,
    )

    # ``provision_and_save`` rejects absent/invalid/reserved remote ports and
    # applies the accepted value before returning. Normalize it once more at
    # this lifecycle boundary so SSH cannot receive a stale or malformed value.
    try:
        remote_port = int(profile.remote_forward_port)
    except (TypeError, ValueError) as exc:
        raise ProvisioningError("provisioning 成功但 remote_port 未正确应用；未启动 SSH 隧道") from exc
    if not 1 <= remote_port <= 65535 or remote_port == 18765:
        raise ProvisioningError("provisioning 成功但 remote_port 无效；未启动 SSH 隧道")
    profile.remote_forward_port = remote_port

    missing = [
        name
        for name in ("ssh_host", "ssh_user", "ssh_identity_file", "ssh_known_hosts")
        if not str(getattr(profile, name, "") or "").strip()
    ]
    if missing:
        raise SSHTunnelError(
            "provisioning 已成功，但 SSH 凭据未配置：缺少 " + ", ".join(missing) + "；未输出 URL"
        )
    try:
        # Validate the accepted port and all credential/file boundaries before
        # spawning a child process. SSHTunnel repeats this validation in start.
        validate_ssh_profile(profile)
        tunnel = SSHTunnel(profile, config_root / "runtime")
        tunnel_status = tunnel.start()
    except SSHTunnelError as exc:
        raise SSHTunnelError(
            "provisioning 已成功，但 SSH 隧道未启动：" + str(exc) + "；未输出 URL"
        ) from exc

    process = getattr(tunnel_status, "process", None)
    if not (
        getattr(tunnel_status, "configured", False)
        and process is not None
        and getattr(process, "running", False)
        and getattr(process, "identity_ok", False)
    ):
        detail = getattr(tunnel_status, "detail", "") or "SSH 隧道进程未 ready"
        raise SupervisorError("provisioning 已成功，但 SSH 隧道未 ready：" + detail + "；未输出 URL")

    try:
        verify_public_endpoint(endpoint_url, profile)
    except (ProvisioningError, ValueError) as exc:
        raise ProvisioningError("provisioning/SSH 已完成，但公网 MCP 验证失败；未输出 URL") from exc

    # Only the endpoint that passed initialize/tools/list/read verification is
    # revealed by this flow.
    return endpoint_url


def main(argv: list[str] | None = None, *, gui_instance: GUIInstance | None = None) -> int:
    args = _parser().parse_args(argv)
    if not any((args.status, args.start, args.provision, args.stop, args.url)):
        owned_instance = False
        if gui_instance is None:
            try:
                gui_instance = GUIInstance(config_file(args.config))
                if not gui_instance.acquire(activate_existing=not args.startup):
                    gui_instance.close()
                    return 0
                owned_instance = True
            except (InstanceError, ConfigError) as exc:
                print(f"错误：{exc}", file=sys.stderr)
                return 2
        try:
            run_kwargs = {"startup_mode": args.startup}
            if gui_instance is not None:
                run_kwargs["instance"] = gui_instance
            run_gui(args.config, **run_kwargs)
            return 0
        finally:
            if owned_instance and gui_instance is not None:
                gui_instance.close()
    try:
        profile = load(args.config)
        config_root = config_file(args.config).parent
        if args.provision:
            # Explicit provisioning is the only non-GUI registration path. It
            # never invents a URL: KTRuntime.url() is the sole endpoint source.
            endpoint = _provision(args, profile, config_root)
            # --provision reveals only the endpoint returned by local KT;
            # provisioning response metadata is never printed.
            print(endpoint)
            return 0
        if args.url:
            # Explicit output is the only CLI path allowed to reveal a URL.
            # Always refresh from the independent KT, not from profile fields.
            runtime = KTRuntime(profile, config_root / "runtime", config_root=config_root)
            print(runtime.url())
            return 0
        supervisor = Supervisor(profile, config_root / "runtime")
        if args.start:
            result = supervisor.start().public_dict()
            if result.get("state") != "running":
                raise SupervisorError(result.get("detail") or "KT 或 SSH 未 ready")
        elif args.stop:
            result = supervisor.stop().public_dict()
        else:
            result = supervisor.status().public_dict()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, KTRuntimeError, ProvisioningError, SSHTunnelError, SupervisorError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
