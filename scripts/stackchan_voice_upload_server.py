#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp_server import audio_processing  # noqa: E402
from mcp_server.audio_server import AUDIO_DIR  # noqa: E402
from mcp_server.stackchan_config import StackchanConfig, load_config  # noqa: E402
from mcp_server.voice_inbox import append_event, resolve_inbox_path  # noqa: E402
from scripts.stackchan_voice_bridge import load_env_file, should_append_to_inbox  # noqa: E402

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_PROMPT_PREFIX = "[Stack-chan语音输入] "


@dataclass(frozen=True)
class ServerOptions:
    lang: str
    max_bytes: int
    inbox_path: Path | None
    wake_url: str
    wake_session_id: str
    wake_token: str
    wake_model: str
    wake_timeout: float
    wake_retries: int
    wake_retry_delay: float
    wake_force: bool
    wake_quiet_minutes: int
    prompt_prefix: str
    wake_words: tuple[str, ...]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def save_uploaded_wav(audio_data: bytes, audio_dir: Path = AUDIO_DIR) -> Path:
    audio_dir.mkdir(parents=True, exist_ok=True)
    wav_path = audio_dir / f"upload_{time.time_ns()}.wav"
    wav_path.write_bytes(audio_data)
    return wav_path


def build_transcript_event(
    *,
    wav_path: Path,
    audio_bytes: int,
    asr_result: dict[str, Any],
    lang: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "transcript",
        "source": "voice_upload",
        "timestamp": timestamp or utc_now(),
        "lang": lang,
        "text": asr_result.get("text", ""),
        "duration": asr_result.get("duration", 0),
        "detected_language": asr_result.get("language", "?"),
        "audio_bytes": audio_bytes,
        "wav_path": str(wav_path),
    }


def process_uploaded_wav(
    audio_data: bytes,
    config: StackchanConfig,
    *,
    lang: str = "zh",
    audio_dir: Path = AUDIO_DIR,
    transcribe_fn=audio_processing.transcribe_audio,
) -> dict[str, Any]:
    if not config.fish_audio_key:
        raise RuntimeError("Fish Audio key is not configured; set FISH_AUDIO_KEY before uploading audio.")

    wav_path = save_uploaded_wav(audio_data, audio_dir)
    asr_result = transcribe_fn(wav_path, lang, config)
    return build_transcript_event(
        wav_path=wav_path,
        audio_bytes=len(audio_data),
        asr_result=asr_result,
        lang=lang,
    )


def frontend_prompt(text: str, prefix: str = DEFAULT_PROMPT_PREFIX) -> str:
    return f"{prefix}{text.strip()}"


def parse_wake_words(raw: str) -> tuple[str, ...]:
    return tuple(word.strip() for word in raw.split(",") if word.strip())


def match_wake_word(text: str, wake_words: tuple[str, ...]) -> tuple[bool, str, str]:
    clean_text = text.strip()
    if not wake_words:
        return True, clean_text, ""

    for word in wake_words:
        if not word:
            continue
        if clean_text.startswith(word):
            stripped = clean_text[len(word) :].lstrip(" ，,。.!！?？:：、")
            return True, stripped or clean_text, word
        marker = f" {word}"
        if marker in clean_text:
            before, after = clean_text.split(marker, 1)
            if not before.strip():
                stripped = after.lstrip(" ，,。.!！?？:：、")
                return True, stripped or clean_text, word
    return False, clean_text, ""


def forward_to_frontend(
    event: dict[str, Any],
    *,
    wake_url: str,
    session_id: str,
    token: str = "",
    model: str = "",
    timeout: float = 10.0,
    retries: int = 0,
    retry_delay: float = 3.0,
    force: bool = True,
    quiet_minutes: int = 0,
    prompt_prefix: str = DEFAULT_PROMPT_PREFIX,
    wake_words: tuple[str, ...] = (),
) -> dict[str, Any]:
    text = str(event.get("text") or "").strip()
    if not text:
        return {"ok": False, "skipped": "empty transcript"}
    matched, prompt_text, matched_word = match_wake_word(text, wake_words)
    if not matched:
        return {"ok": False, "skipped": "wake word not found", "wake_words": list(wake_words)}
    if not wake_url or not session_id:
        return {"ok": False, "skipped": "frontend wake not configured"}

    payload: dict[str, Any] = {
        "session_id": session_id,
        "prompt": frontend_prompt(prompt_text, prompt_prefix),
        "force": force,
        "quiet_minutes": quiet_minutes,
    }
    if model:
        payload["model"] = model

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    attempts = max(0, int(retries)) + 1
    last_result: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(wake_url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            return {"ok": False, "attempts": attempt, "error": str(exc)}

        result: dict[str, Any] = {
            "ok": 200 <= response.status_code < 300,
            "status_code": response.status_code,
            "attempts": attempt,
        }
        try:
            result["body"] = response.json()
        except ValueError:
            result["body"] = response.text[:500]
        if result["ok"]:
            if matched_word:
                result["wake_word"] = matched_word
            return result
        last_result = result
        if response.status_code != 409 or attempt >= attempts:
            break
        time.sleep(max(0.0, retry_delay))

    result = last_result or {"ok": False, "error": "frontend wake failed without response"}
    if matched_word:
        result["wake_word"] = matched_word
    return result


class VoiceUploadServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, *, config: StackchanConfig, options: ServerOptions):
        super().__init__(server_address, handler_class)
        self.config = config
        self.options = options


class VoiceUploadHandler(BaseHTTPRequestHandler):
    server: VoiceUploadServer

    def log_message(self, fmt: str, *args) -> None:
        print(f"[voice-upload] {self.address_string()} - {fmt % args}", flush=True)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path != "/health":
            write_json(self, 404, {"ok": False, "error": "not found"})
            return
        write_json(
            self,
            200,
            {
                "ok": True,
                "service": "stackchan_voice_upload_server",
                "inbox": str(self.server.options.inbox_path) if self.server.options.inbox_path else None,
                "frontend": bool(self.server.options.wake_url and self.server.options.wake_session_id),
            },
        )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/voice/upload":
            write_json(self, 404, {"ok": False, "error": "not found"})
            return

        if not self.server.config.fish_audio_key:
            write_json(
                self,
                503,
                {
                    "ok": False,
                    "error": "Fish Audio key is not configured; set FISH_AUDIO_KEY before uploading audio.",
                },
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            write_json(self, 400, {"ok": False, "error": "invalid Content-Length"})
            return
        if content_length <= 0:
            write_json(self, 400, {"ok": False, "error": "empty audio body"})
            return
        if content_length > self.server.options.max_bytes:
            write_json(self, 413, {"ok": False, "error": "audio payload too large"})
            return

        audio_data = self.rfile.read(content_length)
        if len(audio_data) != content_length:
            write_json(self, 400, {"ok": False, "error": "short audio body"})
            return

        try:
            event = process_uploaded_wav(
                audio_data,
                self.server.config,
                lang=self.server.options.lang,
            )
        except Exception as exc:
            write_json(self, 500, {"ok": False, "error": str(exc)})
            return

        inbox_path = self.server.options.inbox_path
        appended_to_inbox = False
        if inbox_path is not None and should_append_to_inbox(event):
            append_event(event, inbox_path)
            appended_to_inbox = True

        frontend = forward_to_frontend(
            event,
            wake_url=self.server.options.wake_url,
            session_id=self.server.options.wake_session_id,
            token=self.server.options.wake_token,
            model=self.server.options.wake_model,
            timeout=self.server.options.wake_timeout,
            retries=self.server.options.wake_retries,
            retry_delay=self.server.options.wake_retry_delay,
            force=self.server.options.wake_force,
            quiet_minutes=self.server.options.wake_quiet_minutes,
            prompt_prefix=self.server.options.prompt_prefix,
            wake_words=self.server.options.wake_words,
        )

        write_json(
            self,
            200,
            {
                "ok": True,
                "event": event,
                "inbox_appended": appended_to_inbox,
                "frontend": frontend,
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Receive Stack-chan pushed WAV recordings at /voice/upload, transcribe them, "
            "write the local voice inbox, and optionally forward text into migratorybird's /wake endpoint."
        )
    )
    parser.add_argument("--host", default=os.environ.get("STACKCHAN_VOICE_UPLOAD_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("STACKCHAN_VOICE_UPLOAD_PORT", "8767")),
    )
    parser.add_argument("--lang", default=os.environ.get("STACKCHAN_VOICE_LANG", "zh"))
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=int(os.environ.get("STACKCHAN_VOICE_UPLOAD_MAX_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))),
    )
    parser.add_argument(
        "--inbox",
        default=os.environ.get("STACKCHAN_VOICE_INBOX"),
        help="JSONL inbox path. Default: /tmp/stackchan_audio/voice_inbox.jsonl",
    )
    parser.add_argument("--no-inbox", action="store_true", help="Do not append transcripts to inbox")
    parser.add_argument(
        "--wake-url",
        default=os.environ.get("STACKCHAN_FRONTEND_WAKE_URL", ""),
        help="agent-host /wake URL. If omitted, frontend forwarding is skipped.",
    )
    parser.add_argument(
        "--wake-session-id",
        default=os.environ.get("STACKCHAN_FRONTEND_SESSION_ID", ""),
        help="migratorybird frontend session UUID to receive transcripts.",
    )
    parser.add_argument("--wake-token", default=os.environ.get("STACKCHAN_FRONTEND_TOKEN", ""))
    parser.add_argument("--wake-model", default=os.environ.get("STACKCHAN_FRONTEND_MODEL", ""))
    parser.add_argument(
        "--wake-timeout",
        type=float,
        default=float(os.environ.get("STACKCHAN_FRONTEND_TIMEOUT", "10")),
    )
    parser.add_argument(
        "--wake-retries",
        type=int,
        default=int(os.environ.get("STACKCHAN_FRONTEND_RETRIES", "0")),
        help="Retry /wake when agent-host returns 409 busy. Default: 0.",
    )
    parser.add_argument(
        "--wake-retry-delay",
        type=float,
        default=float(os.environ.get("STACKCHAN_FRONTEND_RETRY_DELAY", "3")),
        help="Seconds between 409 busy retries. Default: 3.",
    )
    parser.add_argument(
        "--wake-quiet-minutes",
        type=int,
        default=int(os.environ.get("STACKCHAN_FRONTEND_QUIET_MINUTES", "0")),
    )
    parser.add_argument(
        "--wake-no-force",
        action="store_true",
        help="Respect agent-host quiet_minutes instead of forcing the voice prompt through.",
    )
    parser.add_argument(
        "--prompt-prefix",
        default=os.environ.get("STACKCHAN_FRONTEND_PROMPT_PREFIX", DEFAULT_PROMPT_PREFIX),
    )
    parser.add_argument(
        "--wake-words",
        default=os.environ.get("STACKCHAN_VOICE_WAKE_WORDS", ""),
        help=(
            "Comma-separated activation words. If set, frontend forwarding only happens when "
            "the transcript starts with one of these words; inbox logging still happens."
        ),
    )
    return parser


def main() -> int:
    load_env_file(REPO_ROOT / ".env")
    args = build_parser().parse_args()
    config = load_config()
    wake_url = args.wake_url
    if not wake_url and args.wake_session_id:
        wake_url = "http://127.0.0.1:3200/wake"
    options = ServerOptions(
        lang=args.lang,
        max_bytes=args.max_bytes,
        inbox_path=None if args.no_inbox else resolve_inbox_path(args.inbox),
        wake_url=wake_url,
        wake_session_id=args.wake_session_id,
        wake_token=args.wake_token,
        wake_model=args.wake_model,
        wake_timeout=args.wake_timeout,
        wake_retries=args.wake_retries,
        wake_retry_delay=args.wake_retry_delay,
        wake_force=not args.wake_no_force,
        wake_quiet_minutes=args.wake_quiet_minutes,
        prompt_prefix=args.prompt_prefix,
        wake_words=parse_wake_words(args.wake_words),
    )
    server = VoiceUploadServer((args.host, args.port), VoiceUploadHandler, config=config, options=options)
    print(
        json.dumps(
            {
                "ok": True,
                "service": "stackchan_voice_upload_server",
                "url": f"http://{args.host}:{args.port}/voice/upload",
                "health": f"http://{args.host}:{args.port}/health",
                "inbox": str(options.inbox_path) if options.inbox_path else None,
                "frontend": bool(options.wake_url and options.wake_session_id),
                "wake_words": list(options.wake_words),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(json.dumps({"ok": True, "event": "stop"}, ensure_ascii=False), flush=True)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
