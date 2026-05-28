#include <M5Unified.h>
#include <WebServer.h>
#include <ArduinoJson.h>
#include "http_server.h"
#include "queue_manager.h"
#include "globals.h"
#include "types.h"
#include "servo_service.h"
#include "camera_service.h"
#include "face_service.h"
#include "playback_service.h"
#include "mic_service.h"

static WebServer server(80);

// ── 録音バッファ（PSRAMに確保）
static uint8_t* s_wav_buf   = nullptr;
static size_t   s_wav_size  = 0;
static bool     s_wav_ready = false;

static uint8_t* s_pcm_upload_buf = nullptr;
static size_t   s_pcm_upload_size = 0;
static size_t   s_pcm_upload_capacity = 0;
static bool     s_pcm_upload_ready = false;
static const char* s_pcm_upload_error = nullptr;
static String   s_pcm_diag_session = "";
static long     s_pcm_diag_next_seq = 0;

#define HTTP_PCM_MAX_BYTES (128 * 1024)

// ── モードフラグ（false=APIモード / true=MCPモード）
static bool s_mcp_mode = false;

static void clearPcmUpload() {
    if (s_pcm_upload_buf) {
        free(s_pcm_upload_buf);
    }
    s_pcm_upload_buf = nullptr;
    s_pcm_upload_size = 0;
    s_pcm_upload_capacity = 0;
    s_pcm_upload_ready = false;
}

static bool reservePcmUpload(size_t requiredSize) {
    if (requiredSize <= s_pcm_upload_capacity) {
        return true;
    }

    size_t newCapacity = s_pcm_upload_capacity ? s_pcm_upload_capacity : 8192;
    while (newCapacity < requiredSize) {
        if (newCapacity > HTTP_PCM_MAX_BYTES / 2) {
            newCapacity = HTTP_PCM_MAX_BYTES;
            break;
        }
        newCapacity *= 2;
    }

    uint8_t* newBuf = (uint8_t*)ps_malloc(newCapacity);
    if (!newBuf) {
        return false;
    }
    if (s_pcm_upload_buf && s_pcm_upload_size > 0) {
        memcpy(newBuf, s_pcm_upload_buf, s_pcm_upload_size);
    }
    if (s_pcm_upload_buf) {
        free(s_pcm_upload_buf);
    }
    s_pcm_upload_buf = newBuf;
    s_pcm_upload_capacity = newCapacity;
    return true;
}

// ────────────────────────────────────────────
// POST /play
// body: {"voice_url": "http://..."}
// → AudioTaskをキューに積んで再生
// ────────────────────────────────────────────
static void handlePlay() {
    if (!server.hasArg("plain")) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"no body\"}");
        return;
    }

    JsonDocument doc;
    if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"json parse error\"}");
        return;
    }

    const char* voice_url = doc["voice_url"] | "";
    if (strlen(voice_url) == 0) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"voice_url required\"}");
        return;
    }

    AudioTask task;
    task.voice_id  = String("mcp_") + String(millis());
    task.voice_url = String(voice_url);
    task.priority  = PRIORITY_NORMAL;
    enqueueAudioTask(task);

    Serial.printf("[HTTP] POST /play -> queued: %s\n", voice_url);
    server.send(200, "application/json", "{\"success\":true}");
}

// ────────────────────────────────────────────
// POST /play/pcm
// body: raw 24kHz mono s16le PCM
// ────────────────────────────────────────────
static void handlePlayPcm() {
    if (s_pcm_upload_error) {
        String body = "{\"success\":false,\"error\":\"";
        body += s_pcm_upload_error;
        body += "\"}";
        s_pcm_upload_error = nullptr;
        clearPcmUpload();
        server.send(400, "application/json", body);
        return;
    }
    if (!s_pcm_upload_ready || s_pcm_upload_buf == nullptr || s_pcm_upload_size == 0) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"no pcm body\"}");
        return;
    }

    const size_t pcmSize = s_pcm_upload_size;
    String sessionId = server.arg("session");
    String seqArg = server.arg("seq");
    long seq = seqArg.length() ? seqArg.toInt() : -1;
    bool finalSegment = server.arg("final") == "1" || server.arg("final") == "true";
    uint8_t* pcmData = s_pcm_upload_buf;
    s_pcm_upload_buf = nullptr;
    s_pcm_upload_size = 0;
    s_pcm_upload_capacity = 0;
    s_pcm_upload_ready = false;

    long expectedSeq = s_pcm_diag_next_seq;
    bool newDiagSession = sessionId != s_pcm_diag_session;
    if (newDiagSession) {
        expectedSeq = 0;
    }
    bool seqValid = true;
    if (seq < 0 || seq != expectedSeq) {
        Serial.printf("[HTTP] PCM seq invalid: session=%s got=%ld expected=%ld\n",
                      sessionId.c_str(), seq, expectedSeq);
        seqValid = false;
    }

    if (!seqValid) {
        free(pcmData);
        clearQueuedPcmPlayback();
        server.send(409, "application/json", "{\"success\":false,\"error\":\"pcm seq invalid\"}");
        return;
    }
    PcmPlaybackResult result = startPcmPlayback(pcmData, pcmSize, sessionId, finalSegment);
    if (result != PCM_PLAYBACK_OK && result != PCM_PLAYBACK_QUEUED) {
        if (result != PCM_PLAYBACK_SPEAKER_FAILED) {
            free(pcmData);
        }
        Serial.printf("[HTTP] POST /play/pcm failed -> session=%s seq=%ld bytes=%u final=%s result=%d\n",
                      sessionId.c_str(), seq, (unsigned)pcmSize,
                      finalSegment ? "true" : "false", result);
        if (result == PCM_PLAYBACK_BUSY) {
            server.send(409, "application/json", "{\"success\":false,\"error\":\"playback busy\"}");
        } else if (result == PCM_PLAYBACK_SESSION_MISMATCH) {
            server.send(409, "application/json", "{\"success\":false,\"error\":\"pcm session mismatch\"}");
        } else if (result == PCM_PLAYBACK_SPEAKER_FAILED) {
            server.send(500, "application/json", "{\"success\":false,\"error\":\"speaker failed\"}");
        } else {
            server.send(400, "application/json", "{\"success\":false,\"error\":\"invalid pcm\"}");
        }
        return;
    }

    if (newDiagSession) {
        s_pcm_diag_session = sessionId;
        Serial.printf("[HTTP] PCM diag new session=%s\n", sessionId.c_str());
    }
    s_pcm_diag_next_seq = seq + 1;

    Serial.printf("[HTTP] POST /play/pcm -> session=%s seq=%ld bytes=%u final=%s result=%d queued=%s\n",
                  sessionId.c_str(), seq, (unsigned)pcmSize,
                  finalSegment ? "true" : "false", result,
                  result == PCM_PLAYBACK_QUEUED ? "true" : "false");
    if (result == PCM_PLAYBACK_QUEUED) {
        server.send(202, "application/json", "{\"success\":true,\"queued\":true,\"format\":\"s16le\",\"sample_rate\":24000,\"channels\":1}");
    } else {
        server.send(200, "application/json", "{\"success\":true,\"queued\":false,\"format\":\"s16le\",\"sample_rate\":24000,\"channels\":1}");
    }
}

static void handlePlayPcmRaw() {
    HTTPRaw& raw = server.raw();

    if (raw.status == RAW_START) {
        clearPcmUpload();
        s_pcm_upload_error = nullptr;
        return;
    }

    if (raw.status == RAW_WRITE) {
        if (raw.currentSize > HTTP_PCM_MAX_BYTES - s_pcm_upload_size) {
            s_pcm_upload_error = "pcm too large";
            Serial.println("[HTTP] PCM upload too large");
            clearPcmUpload();
            return;
        }
        size_t newSize = s_pcm_upload_size + raw.currentSize;
        if (!reservePcmUpload(newSize)) {
            s_pcm_upload_error = "pcm alloc failed";
            Serial.println("[HTTP] PCM upload alloc failed");
            clearPcmUpload();
            return;
        }
        memcpy(s_pcm_upload_buf + s_pcm_upload_size, raw.buf, raw.currentSize);
        s_pcm_upload_size += raw.currentSize;
        return;
    }

    if (raw.status == RAW_END) {
        s_pcm_upload_ready = s_pcm_upload_buf != nullptr && s_pcm_upload_size > 0;
        return;
    }

    if (raw.status == RAW_ABORTED) {
        s_pcm_upload_error = "pcm upload aborted";
        clearPcmUpload();
    }
}

// ────────────────────────────────────────────
// POST /mode
// body: {"mode": "mcp"} または {"mode": "api"}
// → MCPモード / APIモードを切り替える
// ────────────────────────────────────────────
static void handleMode() {
    if (!server.hasArg("plain")) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"no body\"}");
        return;
    }

    JsonDocument doc;
    if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"json parse error\"}");
        return;
    }

    const char* mode = doc["mode"] | "";
    if (strcmp(mode, "mcp") == 0) {
        s_mcp_mode = true;
        // 古い録音を完全クリア
        s_wav_ready = false;
        s_wav_size  = 0; 
        if (s_wav_buf) {
            free(s_wav_buf); 
            s_wav_buf = nullptr;
        }
        Serial.println("[HTTP] Mode -> MCP (buffer cleared)");
        server.send(200, "application/json", "{\"success\":true,\"mode\":\"mcp\"}");
    } else if (strcmp(mode, "api") == 0) {
        s_mcp_mode = false;
        Serial.println("[HTTP] Mode -> API");
        server.send(200, "application/json", "{\"success\":true,\"mode\":\"api\"}");
    } else {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"mode must be mcp or api\"}");
    }
}

// ────────────────────────────────────────────
// GET /audio/status
// → {"ready": true/false, "mode": "mcp"|"api"}
// ────────────────────────────────────────────
static void handleAudioStatus() {
    String body = "{\"ready\":";
    body += s_wav_ready ? "true" : "false";
    body += ",\"mode\":\"";
    body += s_mcp_mode ? "mcp" : "api";
    body += "\"}";
    server.send(200, "application/json", body);
}

// ────────────────────────────────────────────
// GET /audio
// → 録音済みWAVをそのまま返す（1回読んだらクリア）
// ────────────────────────────────────────────
static void handleAudio() {
    if (!s_wav_ready || s_wav_buf == nullptr || s_wav_size == 0) {
        server.send(404, "application/json", "{\"success\":false,\"error\":\"no audio\"}");
        return;
    }

    Serial.printf("[HTTP] GET /audio -> %u bytes\n", (unsigned)s_wav_size);
    server.send_P(200, "audio/wav", (const char*)s_wav_buf, s_wav_size);

    // 読んだらクリア（1回限り）
    s_wav_ready = false;
}

// ────────────────────────────────────────────
// POST /move
// body: {"x": float, "y": float, "speed": int}
// → Servo move head (degrees)
// ────────────────────────────────────────────
static void handleMove() {
    if (!server.hasArg("plain")) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"no body\"}");
        return;
    }

    JsonDocument doc;
    if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"json parse error\"}");
        return;
    }

    float x = doc["x"] | 0.0f;
    float y = doc["y"] | 0.0f;
    int speed = doc["speed"] | 50;

    if (!isServoReady()) {
        server.send(503, "application/json", "{\"success\":false,\"error\":\"servo not ready\"}");
        return;
    }
    bool ack = servoMove(x, y, speed);

    Serial.printf("[HTTP] POST /move -> x=%.1f y=%.1f speed=%d\n", x, y, speed);
    server.send(200, "application/json", ack ? "{\"success\":true,\"ack\":true}" : "{\"success\":true,\"ack\":false}");
}

// ────────────────────────────────────────────
// POST /home
// → Return head to center position
// ────────────────────────────────────────────
static void handleHome() {
    if (!isServoReady()) {
        server.send(503, "application/json", "{\"success\":false,\"error\":\"servo not ready\"}");
        return;
    }
    bool ack = servoHome(50);
    Serial.println("[HTTP] POST /home");
    server.send(200, "application/json", ack ? "{\"success\":true,\"ack\":true}" : "{\"success\":true,\"ack\":false}");
}

// ────────────────────────────────────────────
// POST /nod
// → Nod "yes" gesture
// ────────────────────────────────────────────
static void handleNod() {
    if (!isServoReady()) {
        server.send(503, "application/json", "{\"success\":false,\"error\":\"servo not ready\"}");
        return;
    }
    bool ack = servoNod();
    Serial.println("[HTTP] POST /nod");
    server.send(200, "application/json", ack ? "{\"success\":true,\"ack\":true}" : "{\"success\":true,\"ack\":false}");
}

// ────────────────────────────────────────────
// POST /shake
// → Shake "no" gesture
// ────────────────────────────────────────────
static void handleShake() {
    if (!isServoReady()) {
        server.send(503, "application/json", "{\"success\":false,\"error\":\"servo not ready\"}");
        return;
    }
    bool ack = servoShake();
    Serial.println("[HTTP] POST /shake");
    server.send(200, "application/json", ack ? "{\"success\":true,\"ack\":true}" : "{\"success\":true,\"ack\":false}");
}

static void addFeedback(JsonObject obj, const ServoFeedback& fb) {
    obj["ok"] = fb.ok;
    obj["position"] = fb.position;
    obj["speed"] = fb.speed;
    obj["load"] = fb.load;
    obj["voltage"] = fb.voltage;
    obj["temperature"] = fb.temperature;
    obj["moving"] = fb.moving;
    obj["current"] = fb.current;
}

// ────────────────────────────────────────────
// GET /servo/status
// → Servo communication and feedback diagnostics
// ────────────────────────────────────────────
static void handleServoStatus() {
    ServoStatus status = getServoStatus();
    JsonDocument doc;
    doc["ready"] = status.ready;
    doc["last_command_ok"] = status.lastCommandOk;
    doc["last_yaw_raw"] = status.lastYawRaw;
    doc["last_pitch_raw"] = status.lastPitchRaw;
    doc["last_yaw_result"] = status.lastYawResult;
    doc["last_pitch_result"] = status.lastPitchResult;
    doc["last_command_ms"] = status.lastCommandMs;
    doc["gesture_active"] = status.gestureActive;
    doc["gesture"] = status.gestureName;

    JsonObject yaw = doc["yaw"].to<JsonObject>();
    JsonObject pitch = doc["pitch"].to<JsonObject>();
    addFeedback(yaw, status.yaw);
    addFeedback(pitch, status.pitch);

    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
}

// ────────────────────────────────────────────
// GET /playback/status
// → Combined runtime diagnostics for playback, microphone, queues, and memory
// ────────────────────────────────────────────
static void handlePlaybackStatus() {
    PlaybackStatus playback = getPlaybackStatus();
    ServoStatus servo = getServoStatus();

    JsonDocument doc;
    doc["playing"] = playback.playing;
    doc["kind"] = playback.pcm ? "pcm" : (playback.playing ? "wav" : "idle");
    doc["pcm_session"] = playback.pcmSession;
    doc["pcm_final_segment"] = playback.pcmFinalSegment;
    doc["current_bytes"] = playback.currentBytes;
    doc["queued_pcm_bytes"] = playback.queuedPcmBytes;
    doc["queued_pcm_segments"] = playback.queuedPcmSegments;
    doc["audio_queue_depth"] = audioQueue.size();
    doc["started_ms"] = playback.startedMs;
    doc["deadline_ms"] = playback.deadlineMs;
    doc["mic_state"] = getMicStateName();
    doc["mic_resume_requested"] = micResumeRequested;
    doc["servo_ready"] = servo.ready;
    doc["gesture_active"] = servo.gestureActive;
    doc["gesture"] = servo.gestureName;
    doc["free_heap"] = ESP.getFreeHeap();
    doc["free_psram"] = ESP.getFreePsram();

    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
}

// ────────────────────────────────────────────
// POST /face
// body: {"face": "calm"|"thinking"|"happy"|"sleepy"}
// → Switch whale face expression
// ────────────────────────────────────────────
static void handleFace() {
    if (!server.hasArg("plain")) {
        // GET: return current face
        String body = "{\"face\":\"";
        body += getCurrentFaceName();
        body += "\"}";
        server.send(200, "application/json", body);
        return;
    }

    JsonDocument doc;
    if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"json parse error\"}");
        return;
    }

    const char* face = doc["face"] | "";
    WhaleFace wf;

    if (strcmp(face, "calm") == 0)          wf = WHALE_CALM;
    else if (strcmp(face, "thinking") == 0)  wf = WHALE_THINKING;
    else if (strcmp(face, "happy") == 0)     wf = WHALE_HAPPY;
    else if (strcmp(face, "sleepy") == 0)    wf = WHALE_SLEEPY;
    else if (strcmp(face, "shy") == 0)       wf = WHALE_SHY;
    else if (strcmp(face, "smug") == 0)      wf = WHALE_SMUG;
    else if (strcmp(face, "pouty") == 0)     wf = WHALE_POUTY;
    else {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"face must be calm/thinking/happy/sleepy/shy/smug/pouty\"}");
        return;
    }

    setWhaleFace(wf);
    Serial.printf("[HTTP] POST /face -> %s\n", face);
    server.send(200, "application/json", "{\"success\":true}");
}

// ────────────────────────────────────────────
// GET /snapshot
// → Capture JPEG from camera and return it
// ────────────────────────────────────────────
static void handleSnapshot() {
    uint8_t* jpgBuf = nullptr;
    size_t jpgLen = 0;

    if (!captureJpeg(&jpgBuf, &jpgLen, 80)) {
        server.send(500, "application/json", "{\"success\":false,\"error\":\"capture failed\"}");
        return;
    }

    server.send_P(200, "image/jpeg", (const char*)jpgBuf, jpgLen);
    free(jpgBuf);
    Serial.printf("[HTTP] GET /snapshot -> %u bytes JPEG\n", (unsigned)jpgLen);
}

// ────────────────────────────────────────────
// 公開関数
// ────────────────────────────────────────────

bool isMcpMode() {
    return s_mcp_mode;
}

void storeLastRecording(const uint8_t* wav, size_t size) {
    if (s_wav_buf) {
        free(s_wav_buf);
        s_wav_buf = nullptr;
    }
    s_wav_buf = (uint8_t*)ps_malloc(size);
    if (!s_wav_buf) {
        Serial.println("[HTTP] WAV buffer alloc failed");
        s_wav_size  = 0;
        s_wav_ready = false;
        return;
    }
    memcpy(s_wav_buf, wav, size);
    s_wav_size  = size;
    s_wav_ready = true;
    Serial.printf("[HTTP] Stored recording: %u bytes\n", (unsigned)size);
}

void initHttpServer() {
    server.on("/play",         HTTP_POST, handlePlay);
    server.on("/play/pcm",     HTTP_POST, handlePlayPcm, handlePlayPcmRaw);
    server.on("/mode",         HTTP_POST, handleMode);
    server.on("/audio/status", HTTP_GET,  handleAudioStatus);
    server.on("/audio",        HTTP_GET,  handleAudio);
    server.on("/move",         HTTP_POST, handleMove);
    server.on("/home",         HTTP_POST, handleHome);
    server.on("/nod",          HTTP_POST, handleNod);
    server.on("/shake",        HTTP_POST, handleShake);
    server.on("/servo/status", HTTP_GET,  handleServoStatus);
    server.on("/playback/status", HTTP_GET, handlePlaybackStatus);
    server.on("/snapshot",     HTTP_GET,  handleSnapshot);
    server.on("/face",         HTTP_POST, handleFace);
    server.on("/face",         HTTP_GET,  handleFace);
    server.begin();
    Serial.println("[HTTP] Server started on port 80");
}

void handleHttpServer() {
    server.handleClient();
}
