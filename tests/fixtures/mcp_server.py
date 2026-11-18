"""正式集成测试使用的最小 MCP stdio 服务器。"""

from mcp.server.fastmcp import FastMCP

server = FastMCP("ctflow-test")


@server.tool()
def echo(text: str) -> str:
    """返回传入的测试文本。"""
    return f"MCP: {text}"


if __name__ == "__main__":
    server.run(transport="stdio")
