# MCP Client Setup

Stack-chan exposes local stdio MCP and Streamable HTTP MCP. Use stdio for local
coding tools running on the same machine. Use Streamable HTTP when the client
needs a URL, such as remote ChatGPT or Claude web-style connector flows.

## Quick Choice

| Client | Best transport | Setup |
| --- | --- | --- |
| Claude Code | stdio | Add a local command that runs `python -m mcp_server.server`. |
| Claude Desktop | stdio today, `.mcpb` later | Paste local JSON config. A one-click bundle is not published yet. |
| ChatGPT MCP apps | Streamable HTTP | Start `./start-http.sh` and register the HTTPS MCP URL in ChatGPT. |
| Cursor / Windsurf | stdio or HTTP | Paste the same MCP JSON into the client MCP settings. |

## Prerequisites

From the repository root:

```sh
uv sync
cp .env.example .env
```

Edit `.env` with local values:

```sh
STACKCHAN_IP="192.0.2.20"
MAC_IP="192.0.2.10"
FISH_AUDIO_KEY="your_key_here"
```

For stdio clients, either run them from this repository or use absolute paths in
the config examples below.

## Claude Code Stdio Local

Use this shape in the client MCP config:

```json
{
  "mcpServers": {
    "stackchan": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "-m", "mcp_server.server"],
      "cwd": "/absolute/path/to/stackchan",
      "env": {
        "STACKCHAN_IP": "192.0.2.20",
        "MAC_IP": "192.0.2.10",
        "FISH_AUDIO_KEY": "your_key_here"
      }
    }
  }
}
```

If your client does not support `cwd`, set `command` to an absolute Python or
wrapper script path and run it from the repository root.

## Claude Desktop Local

Claude Desktop can use the same stdio server config. On macOS the config file is
usually:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

Minimal entry:

```json
{
  "mcpServers": {
    "stackchan": {
      "command": "uv",
      "args": ["run", "python", "-m", "mcp_server.server"],
      "cwd": "/absolute/path/to/stackchan",
      "env": {
        "STACKCHAN_IP": "192.0.2.20",
        "MAC_IP": "192.0.2.10",
        "FISH_AUDIO_KEY": "your_key_here"
      }
    }
  }
}
```

Claude Desktop also supports MCP bundles (`.mcpb`) for one-click local
installation. Stack-chan should eventually ship one, but this repo does not yet
publish a bundle because it currently depends on a local Python/uv environment,
LAN hardware addresses, and user-specific TTS credentials.

## ChatGPT Remote MCP

ChatGPT needs a reachable HTTPS MCP endpoint. Start local Streamable HTTP mode:

```sh
./start-http.sh
```

For a public tunnel, configure `.env` and opt in explicitly:

```sh
STACKCHAN_PUBLIC_MCP_URL="https://stackchan.example.com/mcp"
STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL="1"
./start-http.sh
```

Then create a custom MCP app/connector in ChatGPT and use
`STACKCHAN_PUBLIC_MCP_URL` as the server URL. Keep write-capable physical tools
restricted to accounts you trust; `stackchan_say`, movement, face changes, and
camera capture affect a live device.

## Cursor And Windsurf

Most IDE MCP clients accept the same stdio JSON shape:

```json
{
  "mcpServers": {
    "stackchan": {
      "command": "uv",
      "args": ["run", "python", "-m", "mcp_server.server"],
      "cwd": "/absolute/path/to/stackchan",
      "env": {
        "STACKCHAN_IP": "192.0.2.20",
        "MAC_IP": "192.0.2.10",
        "FISH_AUDIO_KEY": "your_key_here"
      }
    }
  }
}
```

For clients that prefer URL-based MCP, run:

```sh
./start-http.sh
```

Then use:

```text
http://127.0.0.1:8002/mcp
```

If a client asks for transport type, choose Streamable HTTP for this URL.

## One-Click Install Roadmap

The lowest-friction future path is a Claude Desktop `.mcpb` bundle plus a README
download button. To make that reliable:

1. Add a small bundle manifest with configurable `STACKCHAN_IP`, `MAC_IP`, and
   `FISH_AUDIO_KEY` fields.
2. Decide how to supply Python. Options are requiring `uv`, bundling a Python
   runtime, or adding a small Node wrapper that launches the Python server.
3. Package only host-side MCP files, not firmware secrets or `firmware/src/config.h`.
4. Add a CI job that builds the bundle and runs the MCP contract tests.

Until that exists, the README badges intentionally link to copy-paste setup
instructions rather than pretending to be one-click installers.

## References

- Anthropic Desktop Extensions / `.mcpb`: <https://www.anthropic.com/engineering/desktop-extensions>
- MCPB specification and CLI: <https://github.com/modelcontextprotocol/mcpb>
- ChatGPT developer mode and MCP apps: <https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt>
- OpenAI MCP server guide: <https://developers.openai.com/api/docs/mcp>
