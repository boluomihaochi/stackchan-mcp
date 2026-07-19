#include <Arduino.h>
#include <M5Unified.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>

#include "ws_client_service.h"
#include "config_loader.h"
#include "types.h"
#include "servo_service.h"
#include "camera_service.h"
#include "face_service.h"
#include "playback_service.h"
#include "recording_store.h"

// ─── Binary frame type bytes ────────────────────────────────────────────────
static constexpr uint8_t WS_TYPE_AUDIO    = 0x01;  // WAV  Stackchan→VPS
static constexpr uint8_t WS_TYPE_SNAPSHOT = 0x02;  // JPEG Stackchan→VPS
static constexpr uint8_t WS_TYPE_PCM      = 0x03;  // PCM  VPS→Stackchan

// ─── State ──────────────────────────────────────────────────────────────────
static WebSocketsClient s_ws;
static bool s_connected = false;

// PCM session tracking (mirrors http_server.cpp session bookkeeping)
static String   s_pcm_session = "";
static long     s_pcm_next_seq = 0;

// Periodic audio-ready polling
static unsigned long s_last_audio_check_ms = 0;
static constexpr unsigned long AUDIO_CHECK_INTERVAL_MS = 300;

// Client-side app heartbeat — 焐热蜂窝热点的NAT转发表，防"假死"
// （运营商/手机会划掉不活跃条目，之后双向静默；客户端定时吱一声保活跃）
static unsigned long s_last_hb_ms = 0;
static constexpr unsigned long HB_INTERVAL_MS = 15000;

// Touch de-bounce
static bool s_was_touched = false;

// IMU shake detection
static float s_prev_accel_mag = 1.0f;
static unsigned long s_last_shake_ms = 0;
static constexpr float    SHAKE_THRESHOLD    = 2.8f;
static constexpr unsigned long SHAKE_COOLDOWN_MS = 1200;

// ─── Helpers ────────────────────────────────────────────────────────────────
static void sendText(const char* json) {
    if (s_connected) {
        s_ws.sendTXT(json);
    }
}

static void sendBinary(const uint8_t* data, size_t len) {
    if (s_connected) {
        s_ws.sendBIN(data, len);
    }
}

// ─── Command handlers ────────────────────────────────────────────────────────

static void handlePlayUrl(JsonDocument& doc) {
    const char* url = doc["voice_url"] | "";
    if (strlen(url) == 0) return;
    AudioTask task;
    task.voice_id  = String("ws_") + String(millis());
    task.voice_url = String(url);
    task.priority  = PRIORITY_NORMAL;
    if (enqueueAudioTask(task)) {
        Serial.printf("[WS] play_url queued: %s\n", url);
    } else {
        Serial.println("[WS] play_url: queue full");
    }
}

static void handleFace(JsonDocument& doc) {
    const char* face = doc["face"] | "calm";
    WhaleFace wf;
    if (whaleFaceFromName(face, &wf)) {
        setWhaleFace(wf);
        Serial.printf("[WS] face -> %s\n", face);
    }
}

static void handleMove(JsonDocument& doc) {
    if (!isServoReady()) return;
    float x     = doc["x"]     | 0.0f;
    float y     = doc["y"]     | 0.0f;
    int   speed = doc["speed"] | 20;
    servoMove(x, y, speed);
    Serial.printf("[WS] move x=%.1f y=%.1f speed=%d\n", x, y, speed);
}

static void handleSnapshot() {
    uint8_t* jpg  = nullptr;
    size_t   jlen = 0;
    if (!captureJpeg(&jpg, &jlen, 80)) {
        sendText("{\"event\":\"snapshot_failed\"}");
        return;
    }
    size_t   flen  = 1 + jlen;
    uint8_t* frame = (uint8_t*)malloc(flen);
    if (!frame) {
        free(jpg);
        sendText("{\"event\":\"snapshot_failed\",\"reason\":\"oom\"}");
        return;
    }
    frame[0] = WS_TYPE_SNAPSHOT;
    memcpy(frame + 1, jpg, jlen);
    free(jpg);
    sendBinary(frame, flen);
    free(frame);
    Serial.printf("[WS] snapshot sent: %u bytes\n", (unsigned)jlen);
}

[[maybe_unused]] static void handleAudioPoll() {
    RecordingSnapshot rec = getLastRecording();
    if (!rec.data || rec.size == 0) {
        sendText("{\"event\":\"audio_empty\"}");
        return;
    }
    size_t   flen  = 1 + rec.size;
    uint8_t* frame = (uint8_t*)malloc(flen);
    if (!frame) {
        sendText("{\"event\":\"audio_failed\",\"reason\":\"oom\"}");
        return;
    }
    frame[0] = WS_TYPE_AUDIO;
    memcpy(frame + 1, rec.data, rec.size);
    sendBinary(frame, flen);
    free(frame);
    markLastRecordingConsumed();
    Serial.printf("[WS] audio sent: %u bytes\n", (unsigned)rec.size);
}

// Binary PCM frame from VPS:
//   byte 0:   WS_TYPE_PCM (0x03) — already stripped by caller
//   byte 0:   flags  (bit0 = final)
//   bytes 1-4: seq   (uint32_t LE)
//   bytes 5-8: session_len (uint32_t LE)
//   bytes 9+session_len: raw PCM s16le 24kHz mono
static void handlePcmBinary(const uint8_t* data, size_t len) {
    if (len < 9) {
        Serial.println("[WS] PCM frame too short");
        return;
    }
    bool     final_seg  = (data[0] & 0x01) != 0;
    uint32_t seq        = (uint32_t)data[1]
                        | ((uint32_t)data[2] << 8)
                        | ((uint32_t)data[3] << 16)
                        | ((uint32_t)data[4] << 24);
    uint32_t session_len = (uint32_t)data[5]
                         | ((uint32_t)data[6] << 8)
                         | ((uint32_t)data[7] << 16)
                         | ((uint32_t)data[8] << 24);

    if (9 + session_len > len) {
        Serial.println("[WS] PCM frame: session_len overflows payload");
        return;
    }
    String session_id = String((const char*)(data + 9), session_len);

    const uint8_t* pcm_start = data + 9 + session_len;
    size_t         pcm_len   = len - 9 - session_len;
    if (pcm_len == 0) {
        Serial.println("[WS] PCM frame: empty PCM payload");
        return;
    }

    // Seq continuity check (reset on new session)
    bool new_session = (session_id != s_pcm_session);
    long expected_seq = new_session ? 0 : s_pcm_next_seq;
    if ((long)seq != expected_seq) {
        Serial.printf("[WS] PCM seq mismatch: got=%lu expected=%ld session=%s\n",
                      (unsigned long)seq, expected_seq, session_id.c_str());
        clearQueuedPcmPlayback();
        sendText("{\"event\":\"pcm_seq_error\"}");
        return;
    }

    uint8_t* pcm_buf = (uint8_t*)malloc(pcm_len);
    if (!pcm_buf) {
        Serial.println("[WS] PCM: malloc failed");
        sendText("{\"event\":\"pcm_failed\",\"reason\":\"oom\"}");
        return;
    }
    memcpy(pcm_buf, pcm_start, pcm_len);

    PcmPlaybackResult result = stagePcmPlayback(pcm_buf, pcm_len, session_id, (long)seq, final_seg);
    if (result != PCM_PLAYBACK_OK && result != PCM_PLAYBACK_QUEUED) {
        if (result != PCM_PLAYBACK_SPEAKER_FAILED) free(pcm_buf);
        Serial.printf("[WS] PCM play failed: result=%d session=%s seq=%lu\n",
                      result, session_id.c_str(), (unsigned long)seq);
        sendText("{\"event\":\"pcm_failed\"}");
        return;
    }

    if (new_session) s_pcm_session = session_id;
    s_pcm_next_seq = (long)seq + 1;

    Serial.printf("[WS] PCM: session=%s seq=%lu bytes=%u final=%s\n",
                  session_id.c_str(), (unsigned long)seq, (unsigned)pcm_len,
                  final_seg ? "true" : "false");
}

// ─── JSON command dispatcher ──────────────────────────────────────────────────
static void dispatchTextCmd(uint8_t* payload, size_t length) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length) != DeserializationError::Ok) {
        Serial.println("[WS] JSON parse error");
        return;
    }

    const char* cmd = doc["cmd"] | "";

    if (strcmp(cmd, "ping") == 0) {
        sendText("{\"event\":\"pong\"}");
        return;
    }
    if (strcmp(cmd, "play_url") == 0) { handlePlayUrl(doc); return; }
    if (strcmp(cmd, "face")     == 0) { handleFace(doc);    return; }
    if (strcmp(cmd, "move")     == 0) { handleMove(doc);    return; }
    if (strcmp(cmd, "home")     == 0) { if (isServoReady()) servoHome(20);  return; }
    if (strcmp(cmd, "nod")      == 0) { if (isServoReady()) servoNod();     return; }
    if (strcmp(cmd, "shake")    == 0) { if (isServoReady()) servoShake();   return; }
    if (strcmp(cmd, "snapshot") == 0) { handleSnapshot();   return; }
    if (strcmp(cmd, "sleep_mode") == 0) {
        setWhaleFace(WHALE_SLEEP);
        servoSleep();
        return;
    }
    if (strcmp(cmd, "wake_mode") == 0) {
        setWhaleFace(WHALE_CALM);
        servoWake();
        return;
    }
    // listen 停用（2026-07-19 小诺拍板）：不再上传录音。
    // 256KB 单帧曾是断连元凶；要恢复语音管线时把下一行换回 handleAudioPoll()
    if (strcmp(cmd, "audio_poll") == 0) { sendText("{\"event\":\"audio_disabled\"}"); return; }

    Serial.printf("[WS] unknown cmd: %s\n", cmd);
}

// ─── WebSocket event handler ─────────────────────────────────────────────────
static void wsEventHandler(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED: {
            s_connected = true;
            Serial.printf("[WS] Connected to %s:%d\n", WS_SERVER_HOST, WS_SERVER_PORT);
            // Announce ourselves
            JsonDocument doc;
            doc["event"] = "ready";
            doc["ip"]    = WiFi.localIP().toString();
            String msg;
            serializeJson(doc, msg);
            s_ws.sendTXT(msg);
            break;
        }
        case WStype_DISCONNECTED:
            s_connected = false;
            Serial.println("[WS] Disconnected – will retry");
            break;

        case WStype_TEXT:
            dispatchTextCmd(payload, length);
            break;

        case WStype_BIN:
            if (length < 1) break;
            if (payload[0] == WS_TYPE_PCM) {
                // Strip the type byte; remainder is the PCM header + data
                handlePcmBinary(payload + 1, length - 1);
            }
            break;

        case WStype_ERROR:
            Serial.println("[WS] Protocol error");
            break;

        default:
            break;
    }
}

// ─── Public API ──────────────────────────────────────────────────────────────
void initWsClient() {
    s_ws.begin(WS_SERVER_HOST, WS_SERVER_PORT, "/ws/stackchan");
    s_ws.onEvent(wsEventHandler);
    s_ws.setReconnectInterval(5000);
    // 10s pong timeout x3 retries: 5s/x2 was too strict for the
    // cellular-hotspot -> LA route and killed the link every ~50s
    s_ws.enableHeartbeat(20000, 10000, 3);
    Serial.printf("[WS] Connecting to ws://%s:%d/ws/stackchan\n",
                  WS_SERVER_HOST, WS_SERVER_PORT);
}

void serviceWsClient() {
    s_ws.loop();

    if (!s_connected) return;

    unsigned long now = millis();

    // NAT保活心跳：15秒一个小包，桥收到任何流量都算活
    if (now - s_last_hb_ms >= HB_INTERVAL_MS) {
        s_last_hb_ms = now;
        sendText("{\"event\":\"hb\"}");
    }

    // audio_ready notify disabled — re-enable when STT pipeline is ready
    // if (now - s_last_audio_check_ms >= AUDIO_CHECK_INTERVAL_MS) {
    //     s_last_audio_check_ms = now;
    //     if (hasLastRecording()) {
    //         RecordingSnapshot rec = getLastRecording();
    //         char buf[64];
    //         snprintf(buf, sizeof(buf), "{\"event\":\"audio_ready\",\"size\":%u}",
    //                  (unsigned)rec.size);
    //         sendText(buf);
    //     }
    // }

    // Touch detection (state already updated by M5StackChan.update() in loop)
    bool touched = (M5.Touch.getCount() > 0) && M5.Touch.getDetail(0).wasPressed();
    if (touched && !s_was_touched) {
        auto t = M5.Touch.getDetail(0);
        const char* zone = (t.x < 107) ? "left" : (t.x < 213) ? "center" : "right";
        char buf[64];
        snprintf(buf, sizeof(buf), "{\"event\":\"touch\",\"zone\":\"%s\"}", zone);
        sendText(buf);
    }
    s_was_touched = touched;

    // IMU shake detection
    if (now - s_last_shake_ms >= SHAKE_COOLDOWN_MS) {
        float ax, ay, az;
        if (M5.Imu.getAccel(&ax, &ay, &az)) {
            float mag   = sqrtf(ax * ax + ay * ay + az * az);
            float delta = fabsf(mag - s_prev_accel_mag);
            if (delta > SHAKE_THRESHOLD) {
                JsonDocument doc;
                doc["event"] = "shake";
                JsonObject accel = doc["accel"].to<JsonObject>();
                accel["x"] = ax;
                accel["y"] = ay;
                accel["z"] = az;
                String msg;
                serializeJson(doc, msg);
                sendText(msg.c_str());
                s_last_shake_ms = now;
            }
            s_prev_accel_mag = mag;
        }
    }
}

bool isWsClientConnected() {
    return s_connected;
}
