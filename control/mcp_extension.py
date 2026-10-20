"""Add local Studio tools to the owned MCP app without changing installed packages."""
from __future__ import annotations

from functools import wraps
from pathlib import Path

from mcp import types

from studio_control import StudioControl


def extend_app(app, config_path: Path):
    """Retain KT auth, workspace dispatch, lifespans and job ownership unchanged."""
    if getattr(app.state, "studio_control_enabled", False):
        return app
    endpoint = next(route.endpoint for route in app.routes if route.path == "/mcp")
    server = endpoint.manager.app
    native_list = server.request_handlers[types.ListToolsRequest]
    native_call = server.request_handlers[types.CallToolRequest]
    control = StudioControl(config_path)

    @server.list_tools()
    async def list_tools():
        result = await native_list(types.ListToolsRequest(method="tools/list"))
        tools = list(result.root.tools)
        names = {tool.name for tool in tools}
        if names & control.tool_names:
            raise RuntimeError("Studio tool collides with an installed MCP tool")
        return tools + control.tools()

    @server.call_tool()
    async def call_tool(name, arguments):
        if name in control.tool_names:
            return await control.call(name, arguments or {})
        result = await native_call(types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name=name, arguments=arguments or {}),
        ))
        return result.root

    server.instructions = (server.instructions or "") + (
        "\nStudio control: existing conversations can be searched, read and messaged; "
        "existing goals can be listed, read, updated and transitioned. These Studio "
        "tools do not take workspace_id. They use the existing local Studio on port "
        "8001 and never create replacement conversations or goals. Use session_send "
        "with exact message and a stable request_id, then session_message_read to "
        "verify native persistence. An admission echo is not task completion. Use "
        "goal_get to obtain expected_revision before goal_update/goal_transition. "
        "A paused conversation is resumed only with resume_if_paused=true. Do not "
        "repeat a mutation with a new request_id merely because a reply was lost."
    )
    app.state.studio_control_enabled = True
    app.state.studio_control_tool_names = tuple(sorted(control.tool_names))
    return app


def configure_factory(module, config_path: Path):
    if getattr(module, "_studio_control_factory_enabled", False):
        return
    native_factory = module.create_app

    @wraps(native_factory)
    def create_app(*args, **kwargs):
        return extend_app(native_factory(*args, **kwargs), config_path)

    module.create_app = create_app
    module._studio_control_factory_enabled = True
