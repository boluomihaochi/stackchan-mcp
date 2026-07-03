from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_TELEMETRY_PATH = str(Path(tempfile.gettempdir()) / "stackchan_otel.jsonl")


def new_request_id() -> str:
    return uuid.uuid4().hex


def elapsed_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def timing(**values: int | None) -> dict[str, int]:
    return {key: value for key, value in values.items() if value is not None}


def emit_event(
    event_name: str,
    *,
    body: str = "",
    severity_text: str = "INFO",
    attributes: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    path = Path(os.environ.get("STACKCHAN_OTEL_LOG", DEFAULT_TELEMETRY_PATH))
    attrs: dict[str, Any] = {
        "service.name": os.environ.get("STACKCHAN_SERVICE_NAME", "stackchan-tools"),
    }
    if request_id:
        attrs["stackchan.request_id"] = request_id
    if attributes:
        attrs.update(attributes)

    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "observed_timestamp": datetime.now(UTC).isoformat(),
        "severity_text": severity_text,
        "event_name": event_name,
        "body": body,
        "attributes": attrs,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        # Telemetry must never break the voice path.
        return
