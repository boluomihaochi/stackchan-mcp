import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def load_dotenv(path: Path | None = None) -> None:
    """Load project .env so launchd and direct shell starts share one config."""
    env_path = path or (Path(__file__).resolve().parents[1] / ".env")
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(errors="ignore").splitlines()
    except OSError as exc:
        logger.warning("Could not read %s: %s", env_path, exc)
        return

    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            value = parsed[0] if parsed else ""
        except ValueError:
            value = value.strip().strip("'\"")
        os.environ[key] = value


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%s; using %.2f", name, raw, default)
        return default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%s; using %d", name, raw, default)
        return default


@dataclass(frozen=True)
class StackchanConfig:
    stackchan_ip: str
    stackchan_port: int
    mac_ip: str
    audio_serve_port: int
    tts_engine: str
    audio_mode: str
    save_pcm: bool
    pcm_gain: float
    pcm_limit: float
    pcm_declick_samples: int
    pcm_zero_cross_window: int
    edge_tts_bin: str
    fish_audio_key: str
    fish_audio_model_zh: str
    fish_audio_model_en: str


VALID_AUDIO_MODES = {"auto", "pcm", "wav"}
PCM_SAMPLE_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
PCM_CONTENT_TYPE = "audio/x-raw;format=s16le;rate=24000;channels=1"
MAX_PCM_PAYLOAD_BYTES = 2 * 1024 * 1024
PCM_SEGMENT_BYTES = 48 * 1024
VALID_FACES = ("calm", "thinking", "happy", "sleepy", "shy", "smug", "pouty")
EDGE_VOICES = {
    "zh": "zh-CN-YunxiNeural",
    "en": "en-US-GuyNeural",
}


def load_config() -> StackchanConfig:
    load_dotenv()

    audio_mode = os.environ.get("STACKCHAN_AUDIO_MODE", "auto").lower()
    if audio_mode not in VALID_AUDIO_MODES:
        logger.warning("Invalid STACKCHAN_AUDIO_MODE=%s; using auto", audio_mode)
        audio_mode = "auto"

    pcm_gain = max(0.0, min(env_float("STACKCHAN_PCM_GAIN", 0.75), 1.0))
    pcm_limit = max(0.1, min(env_float("STACKCHAN_PCM_LIMIT", 0.90), 1.0))
    pcm_declick_samples = max(
        0,
        min(env_int("STACKCHAN_PCM_DECLICK_SAMPLES", 64), PCM_SEGMENT_BYTES // PCM_SAMPLE_WIDTH),
    )
    pcm_zero_cross_window = max(
        0,
        min(env_int("STACKCHAN_PCM_ZERO_CROSS_WINDOW", 256), PCM_SEGMENT_BYTES // PCM_SAMPLE_WIDTH),
    )

    return StackchanConfig(
        stackchan_ip=os.environ.get("STACKCHAN_IP", "127.0.0.1"),
        stackchan_port=int(os.environ.get("STACKCHAN_PORT", 80)),
        mac_ip=os.environ.get("MAC_IP", "127.0.0.1"),
        audio_serve_port=int(os.environ.get("AUDIO_SERVE_PORT", 5060)),
        tts_engine=os.environ.get("TTS_ENGINE", "fish-audio"),
        audio_mode=audio_mode,
        save_pcm=os.environ.get("STACKCHAN_SAVE_PCM", "0").lower() in {"1", "true", "yes"},
        pcm_gain=pcm_gain,
        pcm_limit=pcm_limit,
        pcm_declick_samples=pcm_declick_samples,
        pcm_zero_cross_window=pcm_zero_cross_window,
        edge_tts_bin=os.environ.get("EDGE_TTS_BIN", "edge-tts"),
        fish_audio_key=os.environ.get("FISH_AUDIO_KEY", ""),
        fish_audio_model_zh=os.environ.get("FISH_AUDIO_MODEL_ZH", ""),
        fish_audio_model_en=os.environ.get("FISH_AUDIO_MODEL_EN", ""),
    )
