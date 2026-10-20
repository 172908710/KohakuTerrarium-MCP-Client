"""Portable launcher. Invoke with runtime/python[w].exe -I -X utf8 launch.py."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    try:
        from kt_mcp_client.portable import main
        code = main(root=ROOT)
    except Exception as exc:
        # Remain usable even if src/ or the client package is missing.
        import re
        detail = re.sub(r"https?://[^\s\"'<>]+/mcp/[^\s\"'<>]+", "<redacted-endpoint>", str(exc))
        detail = re.sub(r"[A-Za-z0-9_-]{43,128}", "<redacted-value>", detail)
        message = "客户端启动失败：" + detail[:2000]
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "KT MCP Client", 0x10)
        code = 2
    raise SystemExit(code)
