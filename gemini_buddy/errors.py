try:  # mcp 2.x
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError


class Fail(ToolError):
    """An expected failure; unlike other exceptions, its message is shown to the client."""
