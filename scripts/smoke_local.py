"""Explicit real local MCP smoke test; creates and stops only its own endpoint.

Run with runtime/python.exe -I -X utf8 scripts/smoke_local.py.
Uses example.invalid as the external origin, no provisioning or production route.
"""
from pathlib import Path
import json
import re
import socket
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import httpx
from kt_mcp_client.config import DeviceProfile, save
from kt_mcp_client.kt_runtime import KTRuntime
from kt_mcp_client.portable import prepare_environment
from kt_mcp_client.provisioning import EXPECTED_PARITY_TOOLS


def decode(response):
    response.raise_for_status()
    if "application/json" in response.headers.get("content-type", ""):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise RuntimeError("No MCP JSON response")


def main() -> int:
    prepare_environment(ROOT)
    scope = ROOT / (".portable-smoke-" + uuid.uuid4().hex[:10])
    workspace = scope / "workspace"
    workspace.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profile = DeviceProfile(
        workspace_path=str(workspace), workspace_id="smoketest",
        kt_python=str(ROOT / "runtime/python.exe"), kt_home=str(scope / "kt-home"),
        tools_config=str(scope / "tools.yaml"), public_origin="https://example.invalid", local_port=port,
    )
    save(profile, scope)
    runtime = KTRuntime(profile, scope / "runtime", config_root=scope)
    report = {"scope": str(scope), "port": port, "production_modified": False}
    try:
        runtime.setup()
        report["setup"] = True
        started = runtime.start()
        report.update(local_ready=started.local_ready, public_ready=started.public_ready, state=started.cli_status)
        if not started.local_ready:
            raise RuntimeError("Local listener did not become ready")
        endpoint = runtime.url()
        report["actual_endpoint_verified"] = bool(endpoint)
        local = "http://127.0.0.1:" + str(port) + endpoint[endpoint.index("/mcp/"):]
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.post(local, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "portable-smoke", "version": "1"}}})
            initialized = decode(response)
            if "result" not in initialized:
                raise RuntimeError("MCP initialization failed")
            if response.headers.get("mcp-session-id"):
                headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
            headers["MCP-Protocol-Version"] = initialized["result"]["protocolVersion"]
            client.post(local, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            result = decode(client.post(local, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
            report["mcp_tools"] = [tool["name"] for tool in result["result"]["tools"]]
            missing = sorted(EXPECTED_PARITY_TOOLS.difference(report["mcp_tools"]))
            if missing:
                raise RuntimeError("Default tool surface is missing: " + ", ".join(missing))
        report["success"] = True
    except Exception as exc:
        report["success"] = False
        report["error_type"] = type(exc).__name__
        message = re.sub(r"https?://[^\s\"'<>]+/mcp/[^\s\"'<>]+", "<redacted-endpoint>", str(exc))
        report["error"] = re.sub(r"[A-Za-z0-9_-]{43,128}", "<redacted-value>", message)[:4000]
    finally:
        try:
            report["cleanup_state"] = runtime.stop().cli_status
        except Exception as exc:
            report["cleanup_error_type"] = type(exc).__name__
        (scope / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report.get("success") and report.get("cleanup_state") == "stopped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
