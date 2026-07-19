"""
WebSocket bridge server — accepts Stackchan's reverse connection and
exposes a thread-safe synchronous API to the MCP tool functions.

Architecture:
  Stackchan (ESP32-S3) ──WS─→ ws_bridge (VPS :8765)
  MCP tools            ──API──→ StackchanBridge (in-process)

The bridge runs an asyncio event loop in a daemon thread.
MCP tool functions call its synchronous methods (send_command, get_audio, …).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import Future
from typing import Optional

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)

# Binary frame type bytes (must match ws_client_service.h)
_TYPE_AUDIO    = 0x01  # WAV   Stackchan→VPS
_TYPE_SNAPSHOT = 0x02  # JPEG  Stackchan→VPS
_TYPE_PCM      = 0x03  # PCM   VPS→Stackchan

# Session-level PCM tracking (mirrors Stackchan's session bookkeeping)
_pcm_seq: dict[str, int] = {}


class StackchanBridge:
    """Thread-safe bridge between async WS connection and sync MCP tools."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        self.host = host
        self.port = port

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws: WebSocketServerProtocol | None = None

        # One-shot futures set by WS event handler, consumed by sync callers
        self._audio_future: Future[bytes] | None = None
        self._snapshot_future: Future[bytes] | None = None

        # Touch / shake event buffer (ring, max 32)
        self._events: list[dict] = []
        self._events_lock = threading.Lock()

        # Last known Stackchan IP (from the "ready" event)
        self.stackchan_ip: str = ""

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the WS server in a background daemon thread."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ws-bridge")
        self._thread.start()
        logger.info("[WS bridge] server starting on %s:%d", self.host, self.port)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        # Protocol-level pings are OFF: the firmware never answers them, so
        # any ping_interval/ping_timeout combo kills healthy links right on
        # schedule (20+40 => the exact 61s deaths). Liveness is handled by an
        # app-level ping task instead — the firmware answers {"cmd":"ping"}
        # with {"event":"pong"} — see _keepalive().
        async with websockets.serve(self._handle_connection, self.host, self.port,
                                    ping_interval=None):
            logger.info("[WS bridge] listening on ws://%s:%d", self.host, self.port)
            await asyncio.Future()  # run forever

    # ── WebSocket connection handler ────────────────────────────────────────

    async def _handle_connection(self, ws: WebSocketServerProtocol) -> None:
        peer = ws.remote_address
        logger.info("[WS bridge] Stackchan connected from %s", peer)
        self._ws = ws
        self._last_rx = time.time()
        keeper = asyncio.ensure_future(self._keepalive(ws))
        try:
            async for message in ws:
                self._last_rx = time.time()
                if isinstance(message, bytes):
                    await self._on_binary(message)
                else:
                    await self._on_text(message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("[WS bridge] Stackchan disconnected from %s", peer)
        finally:
            keeper.cancel()
            if self._ws is ws:
                self._ws = None

    async def _keepalive(self, ws) -> None:
        """App-level liveness: firmware answers {"cmd":"ping"} with a pong
        event. Any inbound traffic counts as life; 65s of silence after our
        pings means the socket is a zombie — close it so commands stop
        vanishing into it and the firmware reconnects fresh."""
        try:
            while True:
                await asyncio.sleep(20)
                try:
                    await ws.send('{"cmd": "ping"}')
                except websockets.exceptions.ConnectionClosed:
                    return
                if time.time() - self._last_rx > 65:
                    logger.info("[WS bridge] no traffic for 65s, reaping zombie %s",
                                ws.remote_address)
                    await ws.close()
                    return
        except asyncio.CancelledError:
            pass

    async def _on_text(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[WS bridge] invalid JSON: %s", raw[:120])
            return

        ev = event.get("event", "")
        logger.debug("[WS bridge] event: %s", ev)

        if ev == "ready":
            self.stackchan_ip = event.get("ip", "")
            logger.info("[WS bridge] Stackchan ready, IP=%s", self.stackchan_ip)

        elif ev == "audio_ready":
            # Do NOT auto-poll: pushing the ~256KB WAV through a weak uplink
            # stalls the WS heartbeat and kills the connection (the all-day
            # "random" disconnects). Poll explicitly via get_audio() when the
            # voice pipeline actually wants the recording.
            logger.info("[WS bridge] audio_ready (%s bytes) - not auto-polling",
                        event.get("size", "?"))

        elif ev in ("touch", "shake"):
            with self._events_lock:
                self._events.append(event)
                if len(self._events) > 32:
                    self._events.pop(0)
            if ev == "shake":
                await self._on_shake()

        elif ev == "pong":
            pass  # heartbeat response

        elif ev == "audio_empty":
            fut = self._audio_future
            if fut and not fut.done():
                fut.set_exception(RuntimeError("no audio available on Stackchan"))
            self._audio_future = None

        elif ev == "audio_failed":
            fut = self._audio_future
            if fut and not fut.done():
                fut.set_exception(RuntimeError(f"audio fetch failed: {event}"))
            self._audio_future = None

        elif ev == "snapshot_failed":
            fut = self._snapshot_future
            if fut and not fut.done():
                fut.set_exception(RuntimeError(f"snapshot failed: {event}"))
            self._snapshot_future = None

    async def _on_shake(self) -> None:
        """摇多了会生气：10秒内3次shake → 生气脸，8秒后消气回calm。"""
        now = time.time()
        self._shake_times = [t for t in getattr(self, "_shake_times", []) if now - t < 10.0]
        self._shake_times.append(now)
        if len(self._shake_times) >= 3 and now - getattr(self, "_last_anger", 0) > 12.0:
            self._last_anger = now
            self._shake_times.clear()
            logger.info("[WS bridge] shaken 3x in 10s - angry!")
            await self._send_json({"cmd": "face", "face": "pouty"})

            async def _calm_down():
                await asyncio.sleep(8)
                await self._send_json({"cmd": "face", "face": "calm"})
            asyncio.ensure_future(_calm_down())

    async def _on_binary(self, data: bytes) -> None:
        if len(data) < 1:
            return
        msg_type = data[0]
        payload  = data[1:]

        if msg_type == _TYPE_AUDIO:
            fut = self._audio_future
            if fut and not fut.done():
                fut.set_result(payload)
            self._audio_future = None
            logger.debug("[WS bridge] audio received: %d bytes", len(payload))

        elif msg_type == _TYPE_SNAPSHOT:
            fut = self._snapshot_future
            if fut and not fut.done():
                fut.set_result(payload)
            self._snapshot_future = None
            logger.debug("[WS bridge] snapshot received: %d bytes", len(payload))

    async def _send_json(self, obj: dict) -> None:
        if self._ws:
            await self._ws.send(json.dumps(obj))

    async def _send_binary(self, data: bytes) -> None:
        if self._ws:
            await self._ws.send(data)

    # ── Thread-safe helpers ─────────────────────────────────────────────────

    def _run_coro(self, coro, timeout: float = 5.0):
        """Run a coroutine from a sync thread and wait for the result."""
        if self._loop is None:
            raise RuntimeError("Bridge not started")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ── Public synchronous API ───────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def require_connected(self) -> None:
        if not self.connected:
            raise RuntimeError("Stackchan is not connected to the bridge")

    def play_url(self, voice_url: str, timeout: float = 5.0) -> None:
        self.require_connected()
        self._run_coro(self._send_json({"cmd": "play_url", "voice_url": voice_url}), timeout)

    def set_face(self, face: str, timeout: float = 5.0) -> None:
        self.require_connected()
        self._run_coro(self._send_json({"cmd": "face", "face": face}), timeout)

    def servo_move(self, x: float, y: float, speed: int = 50, timeout: float = 5.0) -> None:
        self.require_connected()
        self._run_coro(self._send_json({"cmd": "move", "x": x, "y": y, "speed": speed}), timeout)

    def servo_home(self, timeout: float = 5.0) -> None:
        self.require_connected()
        self._run_coro(self._send_json({"cmd": "home"}), timeout)

    def servo_nod(self, timeout: float = 5.0) -> None:
        self.require_connected()
        self._run_coro(self._send_json({"cmd": "nod"}), timeout)

    def servo_shake(self, timeout: float = 5.0) -> None:
        self.require_connected()
        self._run_coro(self._send_json({"cmd": "shake"}), timeout)

    def request_snapshot(self, wait_timeout: float = 15.0) -> bytes:
        """Send snapshot command and wait for JPEG bytes."""
        self.require_connected()
        # Create a concurrent.futures.Future in the bridge thread's loop
        fut: Future[bytes] = Future()
        self._loop.call_soon_threadsafe(setattr, self, "_snapshot_future", fut)
        self._run_coro(self._send_json({"cmd": "snapshot"}), timeout=5.0)
        return fut.result(timeout=wait_timeout)

    def get_audio(self, wait_timeout: float = 20.0) -> bytes:
        """
        Wait for the next recorded audio WAV from Stackchan.
        The bridge auto-polls when it receives audio_ready; this just
        waits for the WAV bytes to arrive.
        """
        self.require_connected()
        fut: Future[bytes] = Future()
        self._loop.call_soon_threadsafe(setattr, self, "_audio_future", fut)
        return fut.result(timeout=wait_timeout)

    def send_pcm_chunk(self, session_id: str, seq: int, pcm_data: bytes,
                       final: bool = False, timeout: float = 10.0) -> None:
        """Send a raw PCM chunk (s16le 24kHz mono) to Stackchan for playback."""
        self.require_connected()
        session_bytes = session_id.encode("utf-8")
        slen = len(session_bytes)
        header = bytes([
            _TYPE_PCM,
            0x01 if final else 0x00,           # flags
            seq & 0xFF, (seq >> 8) & 0xFF,     # seq LE uint32
            (seq >> 16) & 0xFF, (seq >> 24) & 0xFF,
            slen & 0xFF, (slen >> 8) & 0xFF,   # session_len LE uint32
            (slen >> 16) & 0xFF, (slen >> 24) & 0xFF,
        ])
        frame = header + session_bytes + pcm_data
        self._run_coro(self._send_binary(frame), timeout)

    def pop_events(self) -> list[dict]:
        """Drain and return all buffered touch/shake events."""
        with self._events_lock:
            events, self._events = self._events, []
        return events


# ── Singleton instance ────────────────────────────────────────────────────────

_bridge: Optional[StackchanBridge] = None


def get_bridge(host: str = "0.0.0.0", port: int = 8765) -> StackchanBridge:
    """Return (and lazily start) the singleton bridge."""
    global _bridge
    if _bridge is None:
        _bridge = StackchanBridge(host=host, port=port)
        _bridge.start()
    return _bridge
