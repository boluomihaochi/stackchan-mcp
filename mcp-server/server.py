"""
stackchan-mcp: MCP server for Stack-chan voice control.
Lets any Claude window speak through Stack-chan and listen via its microphone.

Architecture:
  Claude (any window) → MCP tool call → this server
    → TTS (edge-tts / Fish Audio) → WAV file
    → HTTP serve → M5Stack downloads & plays

Usage:
  python server.py                     # stdio mode (for Claude Code CLI)
  python server.py --http --port 8001  # HTTP mode (for Claude Chat/Cowork)
"""

import logging
import os
import struct
import subprocess
import sys as _sys
import threading
import time
import uuid
import wave
from contextlib import suppress
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP, Image

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%s; using %.2f", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%s; using %d", name, raw, default)
        return default

# ── Configuration ──────────────────────────────────────────
STACKCHAN_IP = os.environ.get("STACKCHAN_IP", "192.0.2.20")
STACKCHAN_PORT = int(os.environ.get("STACKCHAN_PORT", 80))
MAC_IP = os.environ.get("MAC_IP", "10.83.20.149")
AUDIO_SERVE_PORT = int(os.environ.get("AUDIO_SERVE_PORT", 5060))

# TTS settings
TTS_ENGINE = os.environ.get("TTS_ENGINE", "fish-audio")  # "edge-tts" or "fish-audio"
STACKCHAN_AUDIO_MODE = os.environ.get("STACKCHAN_AUDIO_MODE", "auto").lower()
STACKCHAN_SAVE_PCM = os.environ.get("STACKCHAN_SAVE_PCM", "0").lower() in {"1", "true", "yes"}
PCM_GAIN = _env_float("STACKCHAN_PCM_GAIN", 0.75)
PCM_LIMIT = _env_float("STACKCHAN_PCM_LIMIT", 0.90)
PCM_DECLICK_SAMPLES = _env_int("STACKCHAN_PCM_DECLICK_SAMPLES", 64)
PCM_ZERO_CROSS_WINDOW = _env_int("STACKCHAN_PCM_ZERO_CROSS_WINDOW", 256)
EDGE_TTS_BIN = os.environ.get("EDGE_TTS_BIN", "/Users/Isa/Kokoro-TTS-Local/venv/bin/edge-tts")
FISH_AUDIO_KEY = os.environ.get("FISH_AUDIO_KEY", "")
FISH_AUDIO_MODEL_ZH = os.environ.get("FISH_AUDIO_MODEL_ZH", "411d04608a3a498192e16724689e7993")  # 夏以昼
FISH_AUDIO_MODEL_EN = os.environ.get("FISH_AUDIO_MODEL_EN", "a1e3e14176b0496c84e6009d672c23f8")  # Nick Valentine
PCM_SAMPLE_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
PCM_CONTENT_TYPE = "audio/x-raw;format=s16le;rate=24000;channels=1"
MAX_PCM_PAYLOAD_BYTES = 2 * 1024 * 1024
PCM_SEGMENT_BYTES = 48 * 1024
VALID_AUDIO_MODES = {"auto", "pcm", "wav"}

if STACKCHAN_AUDIO_MODE not in VALID_AUDIO_MODES:
    logger.warning("Invalid STACKCHAN_AUDIO_MODE=%s; using auto", STACKCHAN_AUDIO_MODE)
    STACKCHAN_AUDIO_MODE = "auto"
PCM_GAIN = max(0.0, min(PCM_GAIN, 1.0))
PCM_LIMIT = max(0.1, min(PCM_LIMIT, 1.0))
PCM_DECLICK_SAMPLES = max(0, min(PCM_DECLICK_SAMPLES, PCM_SEGMENT_BYTES // PCM_SAMPLE_WIDTH))
PCM_ZERO_CROSS_WINDOW = max(0, min(PCM_ZERO_CROSS_WINDOW, PCM_SEGMENT_BYTES // PCM_SAMPLE_WIDTH))


class PcmPlaybackError(RuntimeError):
    def __init__(self, message: str, *, started: bool = False):
        super().__init__(message)
        self.started = started

# Voice mapping for edge-tts
EDGE_VOICES = {
    "zh": "zh-CN-YunxiNeural",
    "en": "en-US-GuyNeural",
}

# Audio directory (fixed path so both stdio & HTTP instances share it)
AUDIO_DIR = Path("/tmp/stackchan_audio")
AUDIO_DIR.mkdir(exist_ok=True)
TEMP_AUDIO_DIR = AUDIO_DIR / ".tmp"
TEMP_AUDIO_DIR.mkdir(exist_ok=True)

# ── Audio HTTP Server (serves WAV files to M5Stack) ───────
class QuietHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves from AUDIO_DIR without printing logs."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(AUDIO_DIR), **kwargs)
    def log_message(self, format, *args):
        pass  # suppress logs

_http_server = None
_http_thread = None

def start_audio_server():
    global _http_server, _http_thread
    if _http_server is not None:
        return
    try:
        _http_server = HTTPServer(("0.0.0.0", AUDIO_SERVE_PORT), QuietHandler)
        _http_thread = threading.Thread(target=_http_server.serve_forever, daemon=True)
        _http_thread.start()
    except OSError:
        pass  # Port already in use (another instance is serving)

def audio_url(filename: str) -> str:
    return f"http://{MAC_IP}:{AUDIO_SERVE_PORT}/{filename}"

# ── TTS Functions ─────────────────────────────────────────
def _new_tts_stem() -> str:
    return f"tts_{int(time.time() * 1000)}_{uuid.uuid4().hex}"


def validate_playback_wav(wav_path: Path) -> None:
    """Validate the WAV contract expected by Stack-chan playback."""
    try:
        with wave.open(str(wav_path), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            compression = wav.getcomptype()
            frame_count = wav.getnframes()

            if (
                compression != "NONE"
                or channels != 1
                or sample_rate != 24000
                or sample_width != 2
            ):
                raise ValueError(
                    "unsupported WAV format: "
                    f"compression={compression} channels={channels} "
                    f"rate={sample_rate} width={sample_width}"
                )
            if frame_count <= 0:
                raise ValueError("WAV has no audio frames")

            pcm = wav.readframes(frame_count)
            expected_bytes = frame_count * channels * sample_width
            if len(pcm) != expected_bytes:
                raise ValueError(
                    f"truncated WAV data: got={len(pcm)} expected={expected_bytes}"
                )
    except (EOFError, wave.Error) as exc:
        raise ValueError(f"invalid WAV file: {exc}") from exc


def publish_validated_wav(temp_wav_path: Path, final_stem: str) -> Path:
    validate_playback_wav(temp_wav_path)
    final_path = AUDIO_DIR / f"{final_stem}.wav"
    os.replace(temp_wav_path, final_path)
    return final_path


def tts_edge(text: str, lang: str = "zh") -> Path:
    """Generate WAV using edge-tts."""
    voice = EDGE_VOICES.get(lang, EDGE_VOICES["zh"])
    stem = _new_tts_stem()
    mp3_path = TEMP_AUDIO_DIR / f"{stem}.mp3"
    temp_wav_path = TEMP_AUDIO_DIR / f"{stem}.wav"

    try:
        # Generate MP3
        subprocess.run([
            EDGE_TTS_BIN, "--voice", voice,
            "--text", text,
            "--write-media", str(mp3_path),
        ], check=True, capture_output=True)

        # Convert to WAV (24kHz 16-bit mono for M5Stack)
        subprocess.run([
            "ffmpeg", "-y", "-i", str(mp3_path),
            "-ar", "24000", "-ac", "1", "-sample_fmt", "s16",
            str(temp_wav_path),
        ], check=True, capture_output=True)

        return publish_validated_wav(temp_wav_path, stem)
    finally:
        mp3_path.unlink(missing_ok=True)
        temp_wav_path.unlink(missing_ok=True)


def tts_fish(text: str, lang: str = "zh") -> Path:
    """Generate WAV using Fish Audio API."""
    model_id = FISH_AUDIO_MODEL_ZH if lang == "zh" else FISH_AUDIO_MODEL_EN
    stem = _new_tts_stem()
    raw_path = TEMP_AUDIO_DIR / f"{stem}_raw.wav"
    temp_wav_path = TEMP_AUDIO_DIR / f"{stem}.wav"

    try:
        # Call Fish Audio API
        resp = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {FISH_AUDIO_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "reference_id": model_id,
                "format": "wav",
                "sample_rate": 24000,
            },
            timeout=30,
        )
        resp.raise_for_status()

        # Fish Audio might return different sample rates, ensure 24kHz mono
        raw_path.write_bytes(resp.content)

        subprocess.run([
            "ffmpeg", "-y", "-i", str(raw_path),
            "-af", "loudnorm=I=-16:TP=-3:LRA=11,alimiter=limit=0.9:attack=0.1:release=50",
            "-ar", "24000", "-ac", "1", "-sample_fmt", "s16",
            str(temp_wav_path),
        ], check=True, capture_output=True)

        return publish_validated_wav(temp_wav_path, stem)
    finally:
        raw_path.unlink(missing_ok=True)
        temp_wav_path.unlink(missing_ok=True)


def generate_tts(text: str, lang: str = "zh") -> Path:
    """Generate TTS audio using configured engine."""
    if TTS_ENGINE == "fish-audio" and FISH_AUDIO_KEY:
        return tts_fish(text, lang)
    return tts_edge(text, lang)


def validate_pcm_contract(sample_rate: int, channels: int, sample_width: int) -> None:
    """Validate the raw PCM contract accepted by firmware /play/pcm."""
    if (
        sample_rate != PCM_SAMPLE_RATE
        or channels != PCM_CHANNELS
        or sample_width != PCM_SAMPLE_WIDTH
    ):
        raise ValueError(
            "unsupported PCM format: "
            f"rate={sample_rate} channels={channels} width={sample_width}"
        )


def condition_pcm_chunk(chunk: bytes) -> tuple[bytes, int]:
    """Apply conservative gain and peak limiting to signed 16-bit PCM."""
    if not chunk:
        return chunk, 0
    if len(chunk) % PCM_SAMPLE_WIDTH != 0:
        raise ValueError(f"invalid PCM payload size: {len(chunk)}")

    peak = int(32767 * PCM_LIMIT)
    out = bytearray(len(chunk))
    limited = 0
    for offset in range(0, len(chunk), PCM_SAMPLE_WIDTH):
        sample = struct.unpack_from("<h", chunk, offset)[0]
        scaled = int(sample * PCM_GAIN)
        if scaled > peak:
            scaled = peak
            limited += 1
        elif scaled < -peak:
            scaled = -peak
            limited += 1
        struct.pack_into("<h", out, offset, scaled)
    return bytes(out), limited


def declick_pcm_segment(segment: bytes, previous_tail_sample: int | None) -> tuple[bytes, int]:
    """Ramp the start of a segment from the previous segment's last sample."""
    if previous_tail_sample is None or PCM_DECLICK_SAMPLES == 0:
        return segment, 0
    if len(segment) % PCM_SAMPLE_WIDTH != 0:
        raise ValueError(f"invalid PCM payload size: {len(segment)}")

    sample_count = len(segment) // PCM_SAMPLE_WIDTH
    ramp_samples = min(PCM_DECLICK_SAMPLES, sample_count)
    out = bytearray(segment)
    for index in range(ramp_samples):
        current = struct.unpack_from("<h", segment, index * PCM_SAMPLE_WIDTH)[0]
        weight = (index + 1) / (ramp_samples + 1)
        smoothed = round(previous_tail_sample + (current - previous_tail_sample) * weight)
        struct.pack_into("<h", out, index * PCM_SAMPLE_WIDTH, smoothed)
    return bytes(out), ramp_samples


def choose_pcm_segment_cut(buffer: bytearray, target_bytes: int) -> int:
    """Choose a segment boundary near a low-amplitude sample before target."""
    target_bytes -= target_bytes % PCM_SAMPLE_WIDTH
    if len(buffer) <= target_bytes or PCM_ZERO_CROSS_WINDOW == 0:
        return target_bytes

    target_sample = target_bytes // PCM_SAMPLE_WIDTH
    start_sample = max(1, target_sample - PCM_ZERO_CROSS_WINDOW)
    best_sample = target_sample
    best_score = abs(struct.unpack_from("<h", buffer, target_bytes - PCM_SAMPLE_WIDTH)[0])

    for sample_index in range(start_sample, target_sample):
        prev_sample = struct.unpack_from("<h", buffer, (sample_index - 1) * PCM_SAMPLE_WIDTH)[0]
        sample = struct.unpack_from("<h", buffer, sample_index * PCM_SAMPLE_WIDTH)[0]
        score = abs(sample)
        if (prev_sample <= 0 <= sample) or (prev_sample >= 0 >= sample):
            score = -1
        if score < best_score:
            best_score = score
            best_sample = sample_index
            if score == -1:
                break

    return max(PCM_SAMPLE_WIDTH, best_sample * PCM_SAMPLE_WIDTH)


def iter_fish_pcm_stream(text: str, lang: str = "zh"):
    """Yield Fish Audio TTS as 24kHz mono s16le PCM chunks."""
    validate_pcm_contract(PCM_SAMPLE_RATE, PCM_CHANNELS, PCM_SAMPLE_WIDTH)
    model_id = FISH_AUDIO_MODEL_ZH if lang == "zh" else FISH_AUDIO_MODEL_EN
    resp = requests.post(
        "https://api.fish.audio/v1/tts",
        headers={
            "Authorization": f"Bearer {FISH_AUDIO_KEY}",
            "Content-Type": "application/json",
            "Accept": PCM_CONTENT_TYPE,
        },
        json={
            "text": text,
            "reference_id": model_id,
            "format": "pcm",
            "sample_rate": PCM_SAMPLE_RATE,
        },
        stream=True,
        timeout=30,
    )
    resp.raise_for_status()

    for chunk in resp.iter_content(chunk_size=4096):
        if chunk:
            yield chunk


def can_stream_pcm() -> bool:
    return (
        STACKCHAN_AUDIO_MODE != "wav"
        and TTS_ENGINE == "fish-audio"
        and bool(FISH_AUDIO_KEY)
    )


# ── M5Stack Communication ────────────────────────────────
def stackchan_play(wav_url: str) -> dict:
    """Push audio URL to Stack-chan for playback."""
    resp = requests.post(
        f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/play",
        json={"voice_url": wav_url},
        timeout=5,
    )
    return resp.json()


def stackchan_play_pcm(pcm_chunks) -> dict:
    """Push raw 24kHz mono s16le PCM to Stack-chan in low-latency segments."""
    buffer = bytearray()
    total_size = 0
    last_result = None
    session_id = uuid.uuid4().hex
    segment_index = 0
    started = False
    pending_segment = None
    limited_samples = 0
    declicked_samples = 0
    last_segment_tail_sample = None
    saved_pcm_path = AUDIO_DIR / f"diag_{session_id}.pcm" if STACKCHAN_SAVE_PCM else None
    saved_pcm_file = saved_pcm_path.open("wb") if saved_pcm_path is not None else None

    def post_segment(segment: bytes, *, final: bool) -> dict:
        nonlocal declicked_samples, last_segment_tail_sample, segment_index, started
        if not segment or len(segment) % PCM_SAMPLE_WIDTH != 0:
            raise ValueError(f"invalid PCM payload size: {len(segment)}")
        segment, declicked = declick_pcm_segment(segment, last_segment_tail_sample)
        declicked_samples += declicked
        last_segment_tail_sample = struct.unpack_from("<h", segment, len(segment) - PCM_SAMPLE_WIDTH)[0]
        url = (
            f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/play/pcm"
            f"?session={session_id}&seq={segment_index}&final={1 if final else 0}"
        )
        try:
            resp = requests.post(
                url,
                data=segment,
                headers={"Content-Type": PCM_CONTENT_TYPE},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
        except requests.HTTPError as exc:
            body = getattr(exc.response, "text", "") if exc.response is not None else ""
            raise PcmPlaybackError(
                f"PCM segment HTTP failed: {exc} body={body[:200]}",
                started=started,
            ) from exc
        except ValueError as exc:
            raise PcmPlaybackError(
                f"PCM segment returned invalid JSON: {exc}",
                started=started,
            ) from exc
        except requests.RequestException as exc:
            raise PcmPlaybackError(f"PCM segment request failed: {exc}", started=started) from exc

        if not result.get("success"):
            raise PcmPlaybackError(f"PCM segment play failed: {result}", started=started)
        started = True
        logger.info(
            "Posted PCM segment session=%s seq=%d bytes=%d final=%s queued=%s total=%d declick=%d",
            session_id,
            segment_index,
            len(segment),
            final,
            result.get("queued"),
            total_size,
            declicked,
        )
        segment_index += 1
        return result

    try:
        for chunk in pcm_chunks:
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > MAX_PCM_PAYLOAD_BYTES:
                message = (
                    "PCM payload too large: "
                    f"{total_size} bytes exceeds {MAX_PCM_PAYLOAD_BYTES} byte limit"
                )
                if started:
                    raise PcmPlaybackError(message, started=True)
                raise ValueError(message)
            if saved_pcm_file is not None:
                saved_pcm_file.write(chunk)
            try:
                conditioned_chunk, limited = condition_pcm_chunk(chunk)
            except ValueError as exc:
                if started:
                    raise PcmPlaybackError(str(exc), started=True) from exc
                raise
            limited_samples += limited
            buffer.extend(conditioned_chunk)
            while len(buffer) >= PCM_SEGMENT_BYTES:
                segment_size = PCM_SEGMENT_BYTES - (PCM_SEGMENT_BYTES % PCM_SAMPLE_WIDTH)
                segment_size = choose_pcm_segment_cut(buffer, segment_size)
                if pending_segment is not None:
                    last_result = post_segment(pending_segment, final=False)
                pending_segment = bytes(buffer[:segment_size])
                del buffer[:segment_size]

        if not buffer and pending_segment is None and last_result is None:
            raise ValueError("invalid PCM payload size: 0")
        if len(buffer) % PCM_SAMPLE_WIDTH != 0:
            message = f"invalid PCM payload size: {len(buffer)}"
            if started:
                raise PcmPlaybackError(message, started=True)
            raise ValueError(message)
        if buffer:
            if pending_segment is not None:
                last_result = post_segment(pending_segment, final=False)
            last_result = post_segment(bytes(buffer), final=True)
        elif pending_segment is not None:
            last_result = post_segment(pending_segment, final=True)
    finally:
        if saved_pcm_file is not None:
            saved_pcm_file.close()

    result = last_result or {"success": False, "error": "no pcm"}
    result.setdefault("session", session_id)
    result.setdefault("segments", segment_index)
    result.setdefault("total_bytes", total_size)
    result.setdefault("pcm_gain", PCM_GAIN)
    result.setdefault("pcm_limit", PCM_LIMIT)
    result.setdefault("limited_samples", limited_samples)
    result.setdefault("declick_samples", PCM_DECLICK_SAMPLES)
    result.setdefault("declicked_samples", declicked_samples)
    if saved_pcm_path is not None:
        result.setdefault("saved_pcm", str(saved_pcm_path))
    logger.info(
        "PCM playback complete session=%s segments=%d total=%d gain=%.2f limit=%.2f limited=%d declicked=%d saved=%s",
        session_id,
        segment_index,
        total_size,
        PCM_GAIN,
        PCM_LIMIT,
        limited_samples,
        declicked_samples,
        saved_pcm_path,
    )
    return result


def stackchan_get_audio() -> bytes | None:
    """Fetch recorded audio from Stack-chan (MCP mode)."""
    resp = requests.get(
        f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/audio",
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.content
    return None


def stackchan_audio_status() -> dict:
    """Check if Stack-chan has a recording ready."""
    resp = requests.get(
        f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/audio/status",
        timeout=3,
    )
    return resp.json()


def stackchan_playback_status_raw() -> dict:
    """Fetch playback and runtime diagnostics from Stack-chan firmware."""
    resp = requests.get(
        f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/playback/status",
        timeout=3,
    )
    return resp.json()


def stackchan_move_raw(x: float, y: float, speed: int) -> dict:
    """Send move command to Stack-chan servos."""
    resp = requests.post(
        f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/move",
        json={"x": x, "y": y, "speed": speed},
        timeout=5,
    )
    return resp.json()


def stackchan_gesture(gesture: str) -> dict:
    """Trigger a preset gesture (nod/shake/home)."""
    resp = requests.post(
        f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/{gesture}",
        timeout=5,
    )
    return resp.json()


def stackchan_set_face(face: str) -> dict:
    """Set Stack-chan's face expression."""
    resp = requests.post(
        f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/face",
        json={"face": face},
        timeout=5,
    )
    return resp.json()


def stackchan_snapshot() -> tuple[bytes | None, int]:
    """Capture JPEG from Stack-chan's camera."""
    # Flush the stale frame sitting in the DMA buffer (CAMERA_GRAB_WHEN_EMPTY keeps
    # one pre-captured frame ready; it may be minutes old). The firmware fix in
    # captureJpeg() also handles this, but this MCP-side call guards against old
    # firmware that hasn't been reflashed yet.
    with suppress(Exception):
        requests.get(f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/snapshot", timeout=5)
    resp = requests.get(
        f"http://{STACKCHAN_IP}:{STACKCHAN_PORT}/snapshot",
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.content, len(resp.content)
    return None, 0


def transcribe_audio(wav_path: Path, lang: str = "zh") -> dict:
    """Transcribe audio using Fish Audio ASR. Returns full response dict."""
    with open(wav_path, "rb") as f:
        resp = requests.post(
            "https://api.fish.audio/v1/asr",
            headers={"Authorization": f"Bearer {FISH_AUDIO_KEY}"},
            files={"audio": f},
            data={"language": lang},
            timeout=15,
        )
    resp.raise_for_status()
    return resp.json()


# ── MCP Server ────────────────────────────────────────────
# Parse args early so we can configure FastMCP constructor
_http_mode = "--http" in _sys.argv
_mcp_port = 8002
for _i, _arg in enumerate(_sys.argv):
    if _arg == "--port" and _i + 1 < len(_sys.argv):
        _mcp_port = int(_sys.argv[_i + 1])

mcp = (
    FastMCP("stackchan", host="0.0.0.0", port=_mcp_port)
    if _http_mode
    else FastMCP("stackchan")
)


@mcp.tool()
def stackchan_say(text: str, lang: str = "zh") -> str:
    """
    Speak through Stack-chan's speaker.
    text: what to say
    lang: "zh" for Chinese (default), "en" for English
    Returns confirmation message.
    """
    start_audio_server()

    try:
        pcm_fallback_reason = None
        logger.info(
            "stackchan_say audio_mode=%s tts_engine=%s fish_key=%s save_pcm=%s",
            STACKCHAN_AUDIO_MODE,
            TTS_ENGINE,
            bool(FISH_AUDIO_KEY),
            STACKCHAN_SAVE_PCM,
        )
        if can_stream_pcm():
            try:
                result = stackchan_play_pcm(iter_fish_pcm_stream(text, lang))
                if result.get("success"):
                    diag = (
                        f" session={result.get('session', '?')}"
                        f" segments={result.get('segments', '?')}"
                        f" bytes={result.get('total_bytes', '?')}"
                        f" gain={result.get('pcm_gain', '?')}"
                        f" limited={result.get('limited_samples', '?')}"
                        f" declicked={result.get('declicked_samples', '?')}"
                    )
                    if result.get("saved_pcm"):
                        diag += f" saved={result['saved_pcm']}"
                    return f"🗣️ Stack-chan is saying: \"{text[:60]}{'…' if len(text)>60 else ''}\" [Fish Audio PCM/{lang}{diag}]"
                pcm_fallback_reason = f"PCM play returned {result}"
                if STACKCHAN_AUDIO_MODE == "pcm":
                    return f"❌ PCM play failed: {result}"
                logger.warning("Falling back to WAV TTS: %s", pcm_fallback_reason)
            except PcmPlaybackError as exc:
                if exc.started:
                    logger.error("PCM playback failed after audio started: %s", exc)
                    return f"❌ PCM playback failed after audio started: {exc}"
                pcm_fallback_reason = str(exc)
                if STACKCHAN_AUDIO_MODE == "pcm":
                    logger.error("PCM playback failed in forced PCM mode: %s", exc)
                    return f"❌ PCM playback failed: {exc}"
                logger.warning("Falling back to WAV TTS after PCM failure: %s", exc)
            except Exception as exc:
                # Keep the stable WAV path as a fallback when streaming is unavailable
                # or firmware rejects the PCM endpoint.
                pcm_fallback_reason = str(exc)
                if STACKCHAN_AUDIO_MODE == "pcm":
                    logger.error("PCM playback failed in forced PCM mode: %s", exc)
                    return f"❌ PCM playback failed: {exc}"
                logger.warning("Falling back to WAV TTS after PCM failure: %s", exc)
        elif STACKCHAN_AUDIO_MODE == "pcm":
            return "❌ PCM playback unavailable: TTS_ENGINE must be fish-audio and FISH_AUDIO_KEY must be set"

        wav_path = generate_tts(text, lang)
        validate_playback_wav(wav_path)
        url = audio_url(wav_path.name)
        result = stackchan_play(url)

        if result.get("success"):
            engine = "Fish Audio" if (TTS_ENGINE == "fish-audio" and FISH_AUDIO_KEY) else "edge-tts"
            fallback_note = " (PCM fallback)" if pcm_fallback_reason else ""
            mode_note = f" mode={STACKCHAN_AUDIO_MODE}"
            return f"🗣️ Stack-chan is saying: \"{text[:60]}{'…' if len(text)>60 else ''}\" [{engine} WAV/{lang}{mode_note}]{fallback_note}"
        else:
            return f"❌ Play failed: {result}"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def stackchan_listen(lang: str = "zh") -> str:
    """
    Listen through Stack-chan's microphone.
    Fetches the latest recording and transcribes it to text using Fish Audio ASR.
    lang: "zh" for Chinese (default), "en" for English, "ja" for Japanese
    Returns the transcribed text, or a status message if no recording is ready.
    """
    try:
        status = stackchan_audio_status()
        if not status.get("ready"):
            return "🎤 No recording ready. Stack-chan is listening... (speak to it and try again)"

        audio_data = stackchan_get_audio()
        if audio_data is None:
            return "❌ Failed to fetch audio from Stack-chan"

        # Save the recording
        wav_path = AUDIO_DIR / f"rec_{int(time.time()*1000)}.wav"
        wav_path.write_bytes(audio_data)

        # Transcribe
        asr_result = transcribe_audio(wav_path, lang)
        text = asr_result.get("text", "")
        asr_duration = asr_result.get("duration", 0)
        asr_lang = asr_result.get("language", "?")
        if text:
            return f"👂 Heard ({asr_duration:.1f}s, {asr_lang}): \"{text}\""
        else:
            return f"🎤 Recording captured ({len(audio_data)} bytes, {asr_duration:.1f}s) but ASR returned empty text. Detected language: {asr_lang}. Audio may be too quiet."
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def stackchan_move(x: float = 0, y: float = 0, speed: int = 50) -> str:
    """
    Move Stack-chan's head.
    x: yaw in degrees, -128 (left) to 128 (right), 0 = center
    y: pitch in degrees, 0 (level) to 90 (up)
    speed: 0-100, higher = faster (default 50)
    Returns confirmation message.
    """
    try:
        x = max(-128, min(128, x))
        y = max(0, min(90, y))
        speed = max(0, min(100, speed))
        result = stackchan_move_raw(x, y, speed)
        if result.get("success"):
            return f"🤖 Head moved to x={x:.0f}° y={y:.0f}° (speed {speed}%)"
        else:
            return f"❌ Move failed: {result}"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def stackchan_nod() -> str:
    """Make Stack-chan nod 'yes'. A quick up-down head motion."""
    try:
        result = stackchan_gesture("nod")
        if result.get("success"):
            return "🤖 *nods yes*"
        else:
            return f"❌ Nod failed: {result}"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def stackchan_shake() -> str:
    """Make Stack-chan shake head 'no'. A quick left-right head motion."""
    try:
        result = stackchan_gesture("shake")
        if result.get("success"):
            return "🤖 *shakes head no*"
        else:
            return f"❌ Shake failed: {result}"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def stackchan_face(expression: str = "calm") -> str:
    """
    Change Stack-chan's face expression.
    expression: "calm" (default gentle face), "thinking" (chin on hand, pondering),
                "happy" (closed eyes, whale spout), "sleepy" (Zzz bubbles),
                "shy" (blushing, averted gaze), "smug" (half-lidded, cocky grin),
                "pouty" (puffed cheeks, annoyed huff)
    """
    valid = ["calm", "thinking", "happy", "sleepy", "shy", "smug", "pouty"]
    if expression not in valid:
        return f"❌ Unknown expression. Choose from: {', '.join(valid)}"
    try:
        result = stackchan_set_face(expression)
        if result.get("success"):
            faces = {"calm": "😊", "thinking": "🤔", "happy": "🐋", "sleepy": "😴",
                     "shy": "😳", "smug": "😏", "pouty": "😤"}
            return f"{faces.get(expression, '🤖')} Face: {expression}"
        else:
            return f"❌ Face change failed: {result}"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def stackchan_see() -> list:
    """
    Take a photo through Stack-chan's camera (GC0308, 320x240).
    Returns the image directly so you can see what Stack-chan is looking at.
    """
    try:
        jpeg_data, size = stackchan_snapshot()
        if jpeg_data is None:
            return "❌ Camera capture failed"

        # Also save locally for CLI usage
        img_path = AUDIO_DIR / f"cam_{int(time.time()*1000)}.jpg"
        img_path.write_bytes(jpeg_data)

        # Return image inline (works in both stdio and HTTP mode)
        return [
            Image(data=jpeg_data, format="jpeg"),
            f"📷 Photo captured ({size} bytes). Saved to: {img_path}",
        ]
    except requests.exceptions.ConnectionError:
        return f"❌ Stack-chan offline (cannot reach {STACKCHAN_IP})"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def stackchan_home() -> str:
    """Return Stack-chan's head to center/home position."""
    try:
        result = stackchan_gesture("home")
        if result.get("success"):
            return "🤖 Head returned to home position"
        else:
            return f"❌ Home failed: {result}"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def stackchan_status() -> str:
    """Check Stack-chan's connection status and current mode."""
    try:
        status = stackchan_audio_status()
        return f"✅ Stack-chan online at {STACKCHAN_IP} | Mode: {status.get('mode', '?')} | Recording ready: {status.get('ready', '?')}"
    except requests.exceptions.ConnectionError:
        return f"❌ Stack-chan offline (cannot reach {STACKCHAN_IP})"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def stackchan_playback_status() -> str:
    """Check Stack-chan playback, queue, memory, and gesture diagnostics."""
    try:
        status = stackchan_playback_status_raw()
        return (
            "Playback "
            f"kind={status.get('kind', '?')} "
            f"playing={status.get('playing', '?')} "
            f"pcm_queue={status.get('queued_pcm_segments', '?')}/"
            f"{status.get('queued_pcm_bytes', '?')}B "
            f"audio_queue={status.get('audio_queue_depth', '?')} "
            f"mic={status.get('mic_state', '?')} "
            f"gesture={status.get('gesture', '?')} "
            f"heap={status.get('free_heap', '?')} "
            f"psram={status.get('free_psram', '?')}"
        )
    except requests.exceptions.ConnectionError:
        return f"❌ Stack-chan offline (cannot reach {STACKCHAN_IP})"
    except Exception as e:
        return f"❌ Error: {e}"


# ── Entry Point ───────────────────────────────────────────
if __name__ == "__main__":
    if _http_mode:
        start_audio_server()
        print(f"Stack-chan MCP server starting on HTTP port {_mcp_port}")
        print(f"Audio server on port {AUDIO_SERVE_PORT}")
        print(f"Stack-chan at {STACKCHAN_IP}:{STACKCHAN_PORT}")
        mcp.run(transport="streamable-http")
    else:
        start_audio_server()
        mcp.run(transport="stdio")
