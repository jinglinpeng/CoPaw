"""Credential-free Python stdio MCP server."""

from mcp.server.fastmcp import FastMCP


server = FastMCP("artifact-python")


@server.tool()
def artifact_echo(text: str) -> str:
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
