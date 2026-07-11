#pragma once

// WebSocket client service.
// Stackchan (ESP32-S3) connects outward to the VPS WebSocket server so it
// works from any network without requiring VPS→Stackchan reachability.
//
// Protocol (text frames, JSON):
//   VPS→Stackchan: {"cmd":"play_url","voice_url":"http://..."}
//                  {"cmd":"face","face":"happy"}
//                  {"cmd":"move","x":10.0,"y":-5.0,"speed":50}
//                  {"cmd":"home"} {"cmd":"nod"} {"cmd":"shake"}
//                  {"cmd":"snapshot"} {"cmd":"audio_poll"} {"cmd":"ping"}
//
//   Stackchan→VPS: {"event":"ready","ip":"..."}
//                  {"event":"audio_ready","size":N}
//                  {"event":"touch","zone":"left|center|right"}
//                  {"event":"shake","accel":{"x":...,"y":...,"z":...}}
//                  {"event":"playback_done"}
//                  {"event":"pong"}
//
// Protocol (binary frames):
//   Stackchan→VPS [0x01 + WAV bytes]  — response to audio_poll
//   Stackchan→VPS [0x02 + JPEG bytes] — response to snapshot
//   VPS→Stackchan [0x03 + header + PCM bytes] — PCM audio chunk
//     header: 1 byte flags (bit0=final), 4 bytes seq LE,
//             4 bytes session_len LE, session_len bytes session string

void initWsClient();
void serviceWsClient();
bool isWsClientConnected();
