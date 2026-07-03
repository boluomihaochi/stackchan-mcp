# Contributing

Thanks for helping improve `stackchan-mcp`. This project touches both host-side
Python tooling and firmware that runs on a physical M5Stack CoreS3, so small,
well-tested changes are easiest to review.

## Before You Open A Pull Request

Run the smallest useful check while iterating, then run the broader gate before
handoff.

| Change type | Minimum check | Broader handoff check |
| --- | --- | --- |
| Python or MCP server only | `uv run ruff check .`, `uv run pyright`, and `uv run pytest` | `make lint` |
| MCP tool behavior or guardrails | `make test-mcp` | `make lint` and `make test` |
| Firmware only | `cd firmware && pio run -e m5stack-cores3` | `make lint` and `make test` |
| Firmware parser or utility code | `cd firmware && pio test -e native` | `cd firmware && pio run -e m5stack-cores3` |
| HTTP contract shared by firmware and MCP | `uv run pytest` and `cd firmware && pio run -e m5stack-cores3` | `make lint` and `make test` |
| Face assets under `firmware/data/` | `cd firmware && pio run -t uploadfs` before device use | Document filename/path changes |

The GitHub Actions workflow runs the host Python checks plus firmware native
tests, CoreS3 build, and high-severity `pio check`.

Python type checking uses `pyright` in basic mode for `mcp_server/` and
`scripts/`. Tests intentionally stay outside the typecheck gate so local fake
clients can remain lightweight.

Python tests report coverage for `mcp_server/` and `scripts/`. Coverage is
reported as visibility, not as a hard threshold, until the important host-side
paths have a stronger baseline.

Ruff formatting is available with `uv run ruff format .`, but formatter checks
are not a CI gate until the existing baseline is normalized. If you run the
formatter, make that a style-only PR.

## Local Setup

```sh
uv sync --dev
python -m pip install --upgrade platformio
```

Firmware configuration is intentionally local:

```sh
cd firmware
cp config.h.example src/config.h
```

Edit `firmware/src/config.h` with your Wi-Fi and host settings. Do not commit
the edited file.

For host-side secrets and local network details:

```sh
cp .env.example .env
```

Do not commit `.env`, API keys, voice model ids, upload tokens, public tunnel
hostnames, or private LAN addresses.

## Optional Git Hook

This repository includes an optional pre-commit hook for fast local checks:

```sh
git config core.hooksPath .githooks
```

By default it runs `uv run ruff check .` and `uv run pytest
tests/test_mcp_server.py`. To run the full Makefile gate before a commit:

```sh
STACKCHAN_HOOK_FULL=1 git commit
```

## Hardware And Safety Notes

- The firmware HTTP API is designed for a trusted LAN. Do not expose the CoreS3
  device directly to the internet without adding an authentication layer.
- `GET /audio` consumes and clears the pending recording buffer. Prefer
  `/audio/status` for non-destructive checks.
- Keep MCP tests isolated from live devices. Unit tests should mock HTTP
  clients rather than calling the robot.
- If you change an HTTP endpoint, update the firmware, Python caller, tests,
  and docs together.

## Observability

For non-destructive probes, logs, metric fields, and deployment-local alert
candidates, see `docs/observability.md`.

## Documentation

For non-trivial debugging sessions, add a note under `docs/` using:

```text
docs/<topic>-troubleshooting-YYYY-MM-DD.md
```

Include symptoms, investigation steps, root cause or current best hypothesis,
final fix, and concrete verification commands/results.
