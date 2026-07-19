#!/usr/bin/env python3
"""Standalone runner: WS bridge (8765) + audio server (5060), no MCP transport.

Used while flashing/testing the Stackchan firmware — keeps the reverse-WS
endpoint alive so the device can connect as soon as it boots.
"""
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

from mcp_server.stackchan_config import load_config
from mcp_server.audio_server import start_audio_server
from mcp_server.ws_bridge import get_bridge

config = load_config()
start_audio_server(config.audio_serve_port)
bridge = get_bridge(port=config.ws_bridge_port)
logging.info("bridge up: ws=%d audio=%d", config.ws_bridge_port, config.audio_serve_port)

from mcp_server.face_tracker import FaceTracker
tracker = FaceTracker(bridge)

# ── Local control endpoint (127.0.0.1 only) ──────────────────────────────────
# POST /cmd  {"cmd": "nod"|"shake"|"home"|"face"|"move"|"play_url"|"status", ...}
import json
import time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Commands issued while the link is down are queued and flushed on reconnect,
# so they eventually play instead of vanishing (the hotspot drops constantly).
PENDING_TTL = 90.0
_pending: list = []  # [(expiry_ts, cmd_dict)]
_pending_lock = None  # set below once threading is imported


def _dispatch(req: dict) -> None:
    cmd = req.get("cmd", "")
    if cmd == "nod":
        bridge.servo_nod()
    elif cmd == "shake":
        bridge.servo_shake()
    elif cmd == "home":
        bridge.servo_home()
    elif cmd == "face":
        bridge.set_face(req.get("face", "calm"))
    elif cmd == "move":
        bridge.servo_move(float(req.get("x", 0)), float(req.get("y", 0)),
                          int(req.get("speed", 20)))
    elif cmd == "play_url":
        bridge.play_url(req.get("url", ""))
    else:
        raise ValueError(f"unknown cmd: {cmd}")


class ControlHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/cmd":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            cmd = req.get("cmd", "")
            if cmd == "status":
                self._reply(200, {"connected": bridge.connected,
                                  "pending": len(_pending),
                                  "tracking": tracker.status,
                                  "events": bridge.pop_events()})
                return
            if cmd == "track_start":
                if tracker.running:
                    self._reply(200, {"ok": True, "note": "already tracking"})
                    return
                tracker.start(flip_x=float(req.get("flip_x", 1)),
                              flip_y=float(req.get("flip_y", 1)))
                self._reply(200, {"ok": True, "cmd": cmd})
                return
            if cmd == "track_stop":
                tracker.stop()
                self._reply(200, {"ok": True, "cmd": cmd})
                return
            if cmd == "snapshot":
                if not bridge.connected:
                    self._reply(503, {"error": "link down, try again"})
                    return
                data = bridge.request_snapshot(
                    wait_timeout=float(req.get("timeout", 30)))
                path = Path("/root/stackchan-mcp/snapshots")
                path.mkdir(exist_ok=True)
                fn = path / f"snap_{int(_time.time())}.jpg"
                fn.write_bytes(data)
                self._reply(200, {"ok": True, "path": str(fn),
                                  "bytes": len(data)})
                return
            if not bridge.connected:
                with _pending_lock:
                    _pending.append((_time.time() + PENDING_TTL, req))
                self._reply(200, {"queued": True, "cmd": cmd,
                                  "note": "link down; will replay on reconnect"})
                return
            try:
                _dispatch(req)
            except ValueError as exc:
                self._reply(400, {"error": str(exc)})
                return
            except Exception:
                # send failed mid-flight (link died between check and send)
                with _pending_lock:
                    _pending.append((_time.time() + PENDING_TTL, req))
                self._reply(200, {"queued": True, "cmd": cmd,
                                  "note": "send failed; will replay on reconnect"})
                return
            self._reply(200, {"ok": True, "cmd": cmd})
        except Exception as exc:
            self._reply(500, {"error": str(exc)})


ctrl = ThreadingHTTPServer(("127.0.0.1", 8766), ControlHandler)
logging.info("control endpoint on 127.0.0.1:8766")
import threading
_pending_lock = threading.Lock()
threading.Thread(target=ctrl.serve_forever, daemon=True).start()


def _flush_pending():
    while True:
        _time.sleep(1)
        if not bridge.connected or not _pending:
            continue
        with _pending_lock:
            batch, _pending[:] = _pending[:], []
        now = _time.time()
        for expiry, req in batch:
            if now > expiry:
                logging.info("[queue] dropped expired cmd: %s", req.get("cmd"))
                continue
            try:
                _dispatch(req)
                logging.info("[queue] replayed cmd: %s", req.get("cmd"))
                _time.sleep(0.5)
            except Exception as exc:
                logging.info("[queue] replay failed (%s), requeueing: %s",
                             exc, req.get("cmd"))
                with _pending_lock:
                    _pending.append((expiry, req))


threading.Thread(target=_flush_pending, daemon=True).start()

while True:
    time.sleep(60)
