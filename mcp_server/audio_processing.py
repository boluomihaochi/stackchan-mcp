import asyncio
import base64
import hashlib
import hmac
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path

import requests
import websockets

from .audio_server import AUDIO_DIR, TEMP_AUDIO_DIR
from .stackchan_config import (
    EDGE_VOICES,
    PCM_CHANNELS,
    PCM_CONTENT_TYPE,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    StackchanConfig,
)


def new_tts_stem() -> str:
    return f"tts_{int(time.time() * 1000)}_{uuid.uuid4().hex}"


def raise_for_fish_status(resp: requests.Response) -> None:
    try:
        resp.raise_for_status()
        return
    except requests.HTTPError as exc:
        try:
            detail = resp.text.strip()
        except Exception:
            detail = ""
        if len(detail) > 500:
            detail = detail[:500] + "…"
        message = str(exc)
        if detail:
            message = f"{message}; Fish response: {detail}"
        raise requests.HTTPError(message, response=resp) from exc


def validate_playback_wav(wav_path: Path) -> None:
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
                or sample_rate != PCM_SAMPLE_RATE
                or sample_width != PCM_SAMPLE_WIDTH
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
                raise ValueError(f"truncated WAV data: got={len(pcm)} expected={expected_bytes}")
    except (EOFError, wave.Error) as exc:
        raise ValueError(f"invalid WAV file: {exc}") from exc


def publish_validated_wav(temp_wav_path: Path, final_stem: str) -> Path:
    validate_playback_wav(temp_wav_path)
    final_path = AUDIO_DIR / f"{final_stem}.wav"
    os.replace(temp_wav_path, final_path)
    return final_path


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f"Required executable not found on PATH: {name}")
    return executable


def tts_edge(text: str, lang: str, config: StackchanConfig) -> Path:
    voice = EDGE_VOICES.get(lang, EDGE_VOICES["zh"])
    stem = new_tts_stem()
    mp3_path = TEMP_AUDIO_DIR / f"{stem}.mp3"
    temp_wav_path = TEMP_AUDIO_DIR / f"{stem}.wav"
    ffmpeg_bin = require_executable("ffmpeg")
    try:
        edge_args = [config.edge_tts_bin, "--voice", voice, "--text", text, "--write-media", str(mp3_path)]
        if config.edge_tts_rate:
            edge_args += ["--rate", config.edge_tts_rate]
        if config.edge_tts_pitch:
            edge_args += ["--pitch", config.edge_tts_pitch]
        subprocess.run(  # noqa: S603 - shell=False and executable/args are controlled.
            edge_args,
            check=True,
            capture_output=True,
        )
        subprocess.run(  # noqa: S603 - shell=False and executable/args are controlled.
            [
                ffmpeg_bin,
                "-y",
                "-i",
                str(mp3_path),
                "-ar",
                str(PCM_SAMPLE_RATE),
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                str(temp_wav_path),
            ],
            check=True,
            capture_output=True,
        )
        return publish_validated_wav(temp_wav_path, stem)
    finally:
        mp3_path.unlink(missing_ok=True)
        temp_wav_path.unlink(missing_ok=True)


def tts_fish(text: str, lang: str, config: StackchanConfig) -> Path:
    model_id = config.fish_audio_model_zh if lang == "zh" else config.fish_audio_model_en
    stem = new_tts_stem()
    raw_path = TEMP_AUDIO_DIR / f"{stem}_raw.wav"
    temp_wav_path = TEMP_AUDIO_DIR / f"{stem}.wav"
    ffmpeg_bin = require_executable("ffmpeg")
    try:
        resp = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {config.fish_audio_key}",
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "reference_id": model_id,
                "format": "wav",
                "sample_rate": PCM_SAMPLE_RATE,
            },
            timeout=config.fish_tts_timeout,
        )
        raise_for_fish_status(resp)
        raw_path.write_bytes(resp.content)
        subprocess.run(  # noqa: S603 - shell=False and executable/args are controlled.
            [
                ffmpeg_bin,
                "-y",
                "-i",
                str(raw_path),
                "-af",
                "loudnorm=I=-16:TP=-3:LRA=11,alimiter=limit=0.9:attack=0.1:release=50",
                "-ar",
                str(PCM_SAMPLE_RATE),
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                str(temp_wav_path),
            ],
            check=True,
            capture_output=True,
        )
        return publish_validated_wav(temp_wav_path, stem)
    finally:
        raw_path.unlink(missing_ok=True)
        temp_wav_path.unlink(missing_ok=True)


def generate_tts(text: str, lang: str, config: StackchanConfig) -> Path:
    if config.tts_engine == "fish-audio" and config.fish_audio_key:
        return tts_fish(text, lang, config)
    return tts_edge(text, lang, config)


def validate_pcm_contract(sample_rate: int, channels: int, sample_width: int) -> None:
    if sample_rate != PCM_SAMPLE_RATE or channels != PCM_CHANNELS or sample_width != PCM_SAMPLE_WIDTH:
        raise ValueError(
            f"unsupported PCM format: rate={sample_rate} channels={channels} width={sample_width}"
        )


def condition_pcm_chunk(chunk: bytes, *, gain: float, limit: float) -> tuple[bytes, int]:
    if not chunk:
        return chunk, 0
    if len(chunk) % PCM_SAMPLE_WIDTH != 0:
        raise ValueError(f"invalid PCM payload size: {len(chunk)}")

    peak = int(32767 * limit)
    out = bytearray(len(chunk))
    limited = 0
    for offset in range(0, len(chunk), PCM_SAMPLE_WIDTH):
        sample = struct.unpack_from("<h", chunk, offset)[0]
        scaled = int(sample * gain)
        if scaled > peak:
            scaled = peak
            limited += 1
        elif scaled < -peak:
            scaled = -peak
            limited += 1
        struct.pack_into("<h", out, offset, scaled)
    return bytes(out), limited


def declick_pcm_segment(segment: bytes, previous_tail_sample: int | None, samples: int) -> tuple[bytes, int]:
    if previous_tail_sample is None or samples == 0:
        return segment, 0
    if len(segment) % PCM_SAMPLE_WIDTH != 0:
        raise ValueError(f"invalid PCM payload size: {len(segment)}")

    sample_count = len(segment) // PCM_SAMPLE_WIDTH
    ramp_samples = min(samples, sample_count)
    out = bytearray(segment)
    for index in range(ramp_samples):
        current = struct.unpack_from("<h", segment, index * PCM_SAMPLE_WIDTH)[0]
        weight = (index + 1) / (ramp_samples + 1)
        smoothed = round(previous_tail_sample + (current - previous_tail_sample) * weight)
        struct.pack_into("<h", out, index * PCM_SAMPLE_WIDTH, smoothed)
    return bytes(out), ramp_samples


def choose_pcm_segment_cut(buffer: bytearray, target_bytes: int, zero_cross_window: int) -> int:
    target_bytes -= target_bytes % PCM_SAMPLE_WIDTH
    if len(buffer) <= target_bytes or zero_cross_window == 0:
        return target_bytes

    target_sample = target_bytes // PCM_SAMPLE_WIDTH
    start_sample = max(1, target_sample - zero_cross_window)
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


def iter_fish_pcm_stream(text: str, lang: str, config: StackchanConfig):
    validate_pcm_contract(PCM_SAMPLE_RATE, PCM_CHANNELS, PCM_SAMPLE_WIDTH)
    model_id = config.fish_audio_model_zh if lang == "zh" else config.fish_audio_model_en
    resp = requests.post(
        "https://api.fish.audio/v1/tts",
        headers={
            "Authorization": f"Bearer {config.fish_audio_key}",
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
        timeout=config.fish_tts_timeout,
    )
    raise_for_fish_status(resp)
    for chunk in resp.iter_content(chunk_size=config.fish_stream_chunk_bytes):
        if chunk:
            yield chunk


def _xunfei_auth_url(app_id: str, api_key: str) -> str:
    ts = str(int(time.time()))
    md5 = hashlib.md5((app_id + ts).encode("utf-8")).hexdigest().encode("utf-8")
    sign = base64.b64encode(hmac.new(api_key.encode("utf-8"), md5, hashlib.sha1).digest()).decode("utf-8")
    return f"wss://rtasr.xfyun.cn/v1/ws?appid={app_id}&ts={ts}&signa={sign}"


def _resample_wav_to_16k(wav_path: Path) -> Path:
    """重采样到16kHz单声道，讯飞rtasr要求。"""
    ffmpeg_bin = require_executable("ffmpeg")
    out_path = wav_path.with_suffix(".16k.wav")
    subprocess.run(
        [ffmpeg_bin, "-y", "-i", str(wav_path), "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(out_path)],
        check=True,
        capture_output=True,
    )
    return out_path


async def _xunfei_transcribe_async(wav_path: Path, app_id: str, api_key: str) -> str:
    url = _xunfei_auth_url(app_id, api_key)
    result_parts: list[str] = []

    async with websockets.connect(url) as ws:
        with wave.open(str(wav_path), "rb") as wf:
            pcm = wf.readframes(wf.getnframes())

        chunk_size = 1280  # 40ms @16kHz 16bit mono
        for i in range(0, len(pcm), chunk_size):
            await ws.send(pcm[i : i + chunk_size])
            await asyncio.sleep(0.04)

        await ws.send(json.dumps({"end": True}))

        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=8.0)
            except asyncio.TimeoutError:
                break
            data = json.loads(msg)
            if data.get("action") == "error":
                raise RuntimeError(f"讯飞STT错误: {data}")
            if data.get("action") == "result":
                seg = json.loads(data.get("data", "{}"))
                for word in seg.get("cn", {}).get("st", {}).get("rt", [{}])[0].get("ws", []):
                    for cw in word.get("cw", []):
                        result_parts.append(cw.get("w", ""))
                if seg.get("cn", {}).get("st", {}).get("type") == "0":
                    break

    return "".join(result_parts)


def transcribe_xunfei(wav_path: Path, config: StackchanConfig) -> dict:
    resampled = _resample_wav_to_16k(wav_path)
    try:
        text = asyncio.run(_xunfei_transcribe_async(resampled, config.xunfei_app_id, config.xunfei_api_key))
    finally:
        resampled.unlink(missing_ok=True)
    return {"text": text}


def transcribe_audio(wav_path: Path, lang: str, config: StackchanConfig) -> dict:
    if config.xunfei_app_id and config.xunfei_api_key:
        return transcribe_xunfei(wav_path, config)
    with open(wav_path, "rb") as f:
        resp = requests.post(
            "https://api.fish.audio/v1/asr",
            headers={"Authorization": f"Bearer {config.fish_audio_key}"},
            files={"audio": f},
            data={"language": lang},
            timeout=config.fish_asr_timeout,
        )
    resp.raise_for_status()
    return resp.json()
