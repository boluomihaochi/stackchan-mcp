"""
stackchan-mcp: MCP server for Stack-chan voice control.

Usage:
  python -m mcp_server.server
  python -m mcp_server.server --http --port 8001
"""

import json
import os
import sys

from mcp.server.fastmcp import FastMCP, Image

from .audio_server import start_audio_server
from .mcp_tools import register_tools
from .stackchan_client import StackchanClient
from .stackchan_config import StackchanConfig, config_summary, load_config


def parse_args(argv: list[str]) -> tuple[bool, str, int]:
    http_mode = "--http" in argv
    host = os.environ.get("STACKCHAN_MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("STACKCHAN_MCP_HTTP_PORT", "8002"))
    for index, arg in enumerate(argv):
        if arg == "--host" and index + 1 < len(argv):
            host = argv[index + 1]
        if arg == "--port" and index + 1 < len(argv):
            port = int(argv[index + 1])
    return http_mode, host, port


def create_mcp(
    config: StackchanConfig,
    *,
    http_mode: bool = False,
    host: str = "127.0.0.1",
    port: int = 8002,
):
    client = StackchanClient(config)
    mcp = FastMCP("stackchan", host=host, port=port) if http_mode else FastMCP("stackchan")
    register_tools(mcp, client, config, Image)
    return mcp


if __name__ == "__main__":
    config = load_config()
    http_mode, mcp_host, mcp_port = parse_args(sys.argv)
    mcp = create_mcp(config, http_mode=http_mode, host=mcp_host, port=mcp_port)
    start_audio_server(config.audio_serve_port)
    if http_mode:
        print(
            json.dumps(
                {
                    "ok": True,
                    "service": "stackchan_mcp_server",
                    "http": {"host": mcp_host, "port": mcp_port},
                    "config": config_summary(config),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
