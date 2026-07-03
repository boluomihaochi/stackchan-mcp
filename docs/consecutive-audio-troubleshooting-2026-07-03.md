# Consecutive Audio Troubleshooting - 2026-07-03

## Symptoms

- When several `/play` requests were sent in quick succession, every second
  playback could disappear.
- A two-item WAV queue could reboot the CoreS3 during the transition from the
  first WAV to the second.
- Before the PCM metadata fix, `POST /play/pcm?...` returned:
  `409 {"success":false,"error":"pcm seq invalid"}`.

## Investigation

- Live PCM upload showed firmware raw upload handling was not reliably seeing
  `session`, `seq`, and `final` query parameters:
  `[HTTP] PCM seq invalid: session= got=-1 expected=0`.
- Single WAV playback worked.
- Consecutive WAV playback crashed after the first WAV finished and the second
  one was about to start.
- Serial log before the final fix showed:
  `E (...) I2S: register I2S object to platform failed`, followed by
  `Stack canary watchpoint triggered (spk_task)`.
- Decoding the backtrace with the current ELF pointed at M5Unified speaker
  internals:
  `m5::Speaker_Class::spk_task(void*)` calling `i2s_write()`.

## False Leads

- Retaining playback buffers until the speaker task is fully ended was
  necessary because `playRaw()` does not copy caller-owned PCM/WAV data, but
  retaining multiple old buffers alone did not fix the reboot.
- Delaying microphone resume while more playback work was pending avoided one
  race, but the remaining crash happened before microphone resume.

## Root Cause

- Raw PCM metadata in query parameters can be lost by ESP32 `WebServer` raw
  upload handling, causing valid PCM segments to be rejected.
- For WAV queues, the firmware treated `M5.Speaker.isPlaying() == false` as a
  fully settled playback boundary. At that point the M5Unified speaker task/I2S
  path can still be active. Starting the next WAV could re-enter speaker begin
  and disturb the I2S driver while the old speaker task was still writing,
  causing `spk_task` to panic.

## Fix

- MCP now sends PCM metadata in headers:
  `X-Stackchan-Pcm-Session`, `X-Stackchan-Pcm-Seq`, and
  `X-Stackchan-Pcm-Final`.
- Firmware accepts those headers first and keeps query parameters for
  compatibility.
- Firmware tracks WAV download-in-flight state so queued `/play` requests are
  held in the logical audio queue instead of colliding with the download queue.
- Firmware only resumes the microphone when no playback, download, WAV queue, or
  PCM queue work remains.
- On playback completion, firmware now synchronously ends the M5Unified speaker
  task/I2S path before freeing the buffer and starting the next queued item.
- The logical WAV queue is capped at 16 pending items; additional `/play`
  requests return `503 {"success":false,"error":"play queue full"}`.
- Queued WAV items retain priority ordering, and same-priority items use an
  internal sequence number so consecutive normal-priority speech remains FIFO.

## Verification

- `uv run ruff check mcp_server tests/test_mcp_server.py`: passed.
- `uv run pytest tests/test_mcp_server.py`: 41 passed.
- `cd firmware && pio run -e m5stack-cores3`: passed.
- `cd firmware && pio test -e native`: 10 test cases passed.
- `cd firmware && pio run -e m5stack-cores3 -t upload --upload-port /dev/cu.usbmodem101`: passed.
- Live PCM header test returned `200` and status moved through `kind=pcm`.
- Live two-WAV queue test produced two distinct `started_ms` values and returned
  to idle without reboot.
- Live four-WAV queue test produced four distinct `started_ms` values:
  `38103`, `39786`, `41619`, `43460`, then returned to idle with empty queues.
- Live ten-WAV queue test accepted nine requests, rejected the tenth with
  `503`, and played the accepted same-priority items in request order.
- Serial logs after the final fix showed no `I2S` error and no
  `Guru Meditation`.
