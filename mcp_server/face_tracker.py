"""Slow-loop face tracking over the reverse-WS link.

Snapshot → YuNet detect → incremental servo move → repeat.
The relay round trip caps us at ~1 frame/s, so this is deliberate
slow tracking: the head drifts toward the face rather than snapping.

Coordinate assumptions (flip via params if the head runs the wrong way):
  yaw  +  = head turns toward image-right
  pitch + = head tilts up          (firmware clamps: yaw ±128°, pitch 5–85°)
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_MODEL = Path(__file__).parent.parent / "models" / "face_detection_yunet_2023mar.onnx"

# Camera: 320x240. Rough horizontal FOV ~55° → full-frame offset ≈ 55° yaw.
_FRAME_W, _FRAME_H = 320, 240
_FOV_X_DEG = 55.0
_FOV_Y_DEG = 42.0
_DAMPING = 0.55          # fraction of measured offset corrected per step
_DEADBAND = 0.08         # |offset| below this (normalized) = centered, don't move
_LOST_AFTER = 5          # misses before we consider her gone
_SEARCH_YAWS = [0.0, -40.0, 40.0, -80.0, 80.0]  # sweep pattern when lost


class FaceTracker:
    def __init__(self, bridge) -> None:
        self.bridge = bridge
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._detector = None
        # Head pose estimate (firmware is open-loop; we mirror what we command)
        self.yaw = 0.0
        self.pitch = 45.0
        self.flip_x = 1.0
        self.flip_y = 1.0
        self.status = "off"    # off | tracking | lost | searching
        self.last_seen: float = 0.0

    # ── Public API (called from control endpoint) ───────────────────────────

    def start(self, flip_x: float = 1.0, flip_y: float = 1.0) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("tracker already running")
        self.flip_x, self.flip_y = flip_x, flip_y
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="face-tracker")
        self._thread.start()
        logger.info("[tracker] started (flip_x=%s flip_y=%s)", flip_x, flip_y)

    def stop(self) -> None:
        self._stop.set()
        self.status = "off"
        logger.info("[tracker] stopped")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ── Loop ────────────────────────────────────────────────────────────────

    def _ensure_detector(self):
        if self._detector is None:
            import cv2
            self._detector = cv2.FaceDetectorYN_create(
                str(_MODEL), "", (_FRAME_W, _FRAME_H), 0.6)
        return self._detector

    def _detect(self, jpeg: bytes):
        """Return (cx, cy) of the biggest face in normalized [-1, 1] coords, or None."""
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        det = self._ensure_detector()
        det.setInputSize((w, h))
        _, faces = det.detect(img)
        if faces is None or len(faces) == 0:
            return None
        # biggest face wins
        f = max(faces, key=lambda f: f[2] * f[3])
        cx = (f[0] + f[2] / 2) / w * 2 - 1     # -1 left … +1 right
        cy = (f[1] + f[3] / 2) / h * 2 - 1     # -1 top  … +1 bottom
        return cx, cy

    def _move(self, yaw: float, pitch: float) -> None:
        self.yaw = max(-128.0, min(128.0, yaw))
        self.pitch = max(5.0, min(85.0, pitch))
        self.bridge.servo_move(self.yaw, self.pitch, speed=25)

    def _run(self) -> None:
        misses = 0
        search_i = 0
        while not self._stop.is_set():
            if not self.bridge.connected:
                self.status = "lost"
                time.sleep(3)
                continue
            try:
                jpeg = self.bridge.request_snapshot(wait_timeout=12.0)
            except Exception as exc:
                logger.debug("[tracker] snapshot failed: %s", exc)
                time.sleep(2)
                continue

            hit = None
            try:
                hit = self._detect(jpeg)
            except Exception as exc:
                logger.warning("[tracker] detect error: %s", exc)

            if hit:
                cx, cy = hit
                misses = 0
                search_i = 0
                self.status = "tracking"
                self.last_seen = time.time()
                if abs(cx) > _DEADBAND or abs(cy) > _DEADBAND:
                    dyaw = cx * (_FOV_X_DEG / 2) * _DAMPING * self.flip_x
                    dpitch = -cy * (_FOV_Y_DEG / 2) * _DAMPING * self.flip_y
                    try:
                        self._move(self.yaw + dyaw, self.pitch + dpitch)
                    except Exception as exc:
                        logger.debug("[tracker] move failed: %s", exc)
            else:
                misses += 1
                if misses == _LOST_AFTER:
                    self.status = "lost"
                    logger.info("[tracker] face lost")
                if misses >= _LOST_AFTER and misses % 3 == 0:
                    # lazy search sweep: step through preset yaws
                    self.status = "searching"
                    try:
                        self._move(_SEARCH_YAWS[search_i % len(_SEARCH_YAWS)], 45.0)
                    except Exception:
                        pass
                    search_i += 1

            # pace the loop: relay RTT already adds ~1s; don't hammer the link
            self._stop.wait(1.2)
