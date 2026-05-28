#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <math.h>
#include <queue>
#include "playback_service.h"
#include "globals.h"
#include "config.h"
#include "face_service.h"

static size_t lipSyncOffset = 0;
static unsigned long lastLipMs = 0;
static size_t currentPcmOffset = 0;
static size_t currentPcmSize = 0;
static uint32_t currentSampleRate = 24000;
static uint16_t currentBytesPerFrame = 2;

#define LIPSYNC_INTERVAL_MS   50
#define LIPSYNC_CHUNK_SAMPLES 1024
#define DOWNLOAD_TIMEOUT_MS   10000
#define MAX_WAV_BYTES         (4 * 1024 * 1024)
#define PCM_SAMPLE_RATE       24000
#define PCM_BYTES_PER_SAMPLE  2
#define MAX_PCM_BYTES         (2 * 1024 * 1024)
#define MAX_QUEUED_PCM_BYTES  (2 * 1024 * 1024)

// ── FreeRTOS: URLをCore 0に渡すキュー
//    StringはFreeRTOSキューに乗せられないのでchar配列で渡す
#define MAX_URL_LEN 256
static QueueHandle_t s_downloadQueue = nullptr;

struct PcmBuffer {
    uint8_t* data;
    size_t size;
    String sessionId;
    bool finalSegment;
};

struct DownloadedAudio {
    uint8_t* data;
    size_t size;
};

static std::queue<PcmBuffer> s_pcmQueue;
static size_t s_pcmQueuedBytes = 0;
static bool s_currentPlaybackIsPcm = false;
static String s_currentPcmSessionId = "";
static bool s_currentPcmFinalSegment = false;
static QueueHandle_t s_downloadCompleteQueue = nullptr;
static uint8_t* s_retiredPlaybackData = nullptr;
static size_t s_retiredPlaybackSize = 0;

struct WavInfo {
    size_t dataOffset;
    size_t dataSize;
    uint32_t sampleRate;
    uint16_t channels;
    uint16_t bitsPerSample;
};

static uint16_t readLe16(const uint8_t* p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static uint32_t readLe32(const uint8_t* p) {
    return (uint32_t)p[0] |
           ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static bool parseWavInfo(const uint8_t* data, size_t size, WavInfo* info) {
    if (!data || !info || size < 12) {
        Serial.println("[WAV] Invalid: too small");
        return false;
    }
    if (memcmp(data, "RIFF", 4) != 0 || memcmp(data + 8, "WAVE", 4) != 0) {
        Serial.println("[WAV] Invalid: missing RIFF/WAVE");
        return false;
    }

    bool foundFmt = false;
    bool foundData = false;
    uint16_t audioFormat = 0;
    WavInfo parsed = {};

    size_t offset = 12;
    while (offset + 8 <= size) {
        const uint8_t* chunk = data + offset;
        uint32_t chunkSize = readLe32(chunk + 4);
        size_t payloadOffset = offset + 8;
        size_t nextOffset = payloadOffset + chunkSize + (chunkSize & 1);

        if (payloadOffset > size || chunkSize > size - payloadOffset) {
            Serial.printf("[WAV] Invalid: chunk overflow at %u size=%u\n",
                          (unsigned)offset, (unsigned)chunkSize);
            return false;
        }

        if (memcmp(chunk, "fmt ", 4) == 0) {
            if (chunkSize < 16) {
                Serial.println("[WAV] Invalid: fmt chunk too small");
                return false;
            }
            audioFormat = readLe16(data + payloadOffset);
            parsed.channels = readLe16(data + payloadOffset + 2);
            parsed.sampleRate = readLe32(data + payloadOffset + 4);
            parsed.bitsPerSample = readLe16(data + payloadOffset + 14);
            foundFmt = true;
        } else if (memcmp(chunk, "data", 4) == 0) {
            parsed.dataOffset = payloadOffset;
            parsed.dataSize = chunkSize;
            foundData = true;
        }

        if (nextOffset <= offset) {
            Serial.println("[WAV] Invalid: chunk offset overflow");
            return false;
        }
        offset = nextOffset;
    }

    if (!foundFmt || !foundData) {
        Serial.println("[WAV] Invalid: missing fmt or data chunk");
        return false;
    }
    if (audioFormat != 1 || parsed.channels != 1 ||
        parsed.sampleRate != 24000 || parsed.bitsPerSample != 16) {
        Serial.printf("[WAV] Unsupported: format=%u channels=%u rate=%u bits=%u\n",
                      audioFormat, parsed.channels,
                      (unsigned)parsed.sampleRate, parsed.bitsPerSample);
        return false;
    }
    if (parsed.dataSize == 0 || parsed.dataOffset + parsed.dataSize > size) {
        Serial.println("[WAV] Invalid: bad data chunk");
        return false;
    }

    *info = parsed;
    return true;
}

void clearQueuedPcmPlayback() {
    while (!s_pcmQueue.empty()) {
        PcmBuffer dropped = s_pcmQueue.front();
        s_pcmQueue.pop();
        free(dropped.data);
    }
    s_pcmQueuedBytes = 0;
    Serial.println("[PCM] Queue cleared");
}

static bool enqueuePcmBuffer(uint8_t* pcmData, size_t pcmSize, const String& sessionId, bool finalSegment) {
    if (pcmSize > MAX_QUEUED_PCM_BYTES - s_pcmQueuedBytes) {
        Serial.println("[PCM] Queue full");
        return false;
    }
    s_pcmQueue.push({pcmData, pcmSize, sessionId, finalSegment});
    s_pcmQueuedBytes += pcmSize;
    Serial.printf("[PCM] Queued segment: session=%s bytes=%u queued=%u final=%s\n",
                  sessionId.c_str(), (unsigned)pcmSize,
                  (unsigned)s_pcmQueuedBytes, finalSegment ? "true" : "false");
    return true;
}

static void releaseRetiredPlaybackBuffer() {
    if (!s_retiredPlaybackData) {
        return;
    }
    Serial.printf("[PLAY] Releasing retired playback buffer: bytes=%u\n",
                  (unsigned)s_retiredPlaybackSize);
    free(s_retiredPlaybackData);
    s_retiredPlaybackData = nullptr;
    s_retiredPlaybackSize = 0;
}

void retireCurrentPlaybackBuffer() {
    releaseRetiredPlaybackBuffer();
    if (!currentWavData) {
        return;
    }
    Serial.printf("[PLAY] Retiring playback buffer: bytes=%u speakerPlaying=%s\n",
                  (unsigned)currentWavSize,
                  M5.Speaker.isPlaying() ? "true" : "false");
    s_retiredPlaybackData = currentWavData;
    s_retiredPlaybackSize = currentWavSize;
    currentWavData = nullptr;
    currentWavSize = 0;
}

// ════════════════════════════════════════
//  ダウンロードタスク（loop()とは別タスクで動く）
//  loop()をブロックしないための分離
// ════════════════════════════════════════
static void downloadTask(void* arg) {
    char url[MAX_URL_LEN];
    for (;;) {
        if (xQueueReceive(s_downloadQueue, url, portMAX_DELAY) != pdTRUE) continue;

        uint8_t* data = nullptr;
        size_t   size = 0;

        if (downloadVoice(String(url), &data, &size)) {
            DownloadedAudio completed = {data, size};
            if (!s_downloadCompleteQueue ||
                xQueueSend(s_downloadCompleteQueue, &completed, 0) != pdTRUE) {
                Serial.println("[DOWNLOAD] Complete queue full; dropping audio");
                free(data);
                continue;
            }
            Serial.printf("[DOWNLOAD] Ready: %u bytes\n", (unsigned)size);
        } else {
            Serial.println("[DOWNLOAD] Failed");
//            setFaceExpression(FACE_IDLE);
        }
    }
}

// ════════════════════════════════════════
//  初期化（setup()から呼ぶ）
// ════════════════════════════════════════
void initPlayback() {
    s_downloadQueue = xQueueCreate(4, sizeof(char) * MAX_URL_LEN);
    s_downloadCompleteQueue = xQueueCreate(2, sizeof(DownloadedAudio));
    if (!s_downloadQueue || !s_downloadCompleteQueue) {
        Serial.println("[PLAY] Failed to create playback queues");
        return;
    }
    xTaskCreatePinnedToCore(
        downloadTask,
        "downloadTask",
        8192,
        nullptr,
        1,
        nullptr,
        1   // Core 1
    );
    Serial.println("[PLAY] Download task started on Core 1");
}

// ════════════════════════════════════════
//  再生リクエスト受付（ノンブロッキング）
//  enqueueAudioTask()から呼ばれる
// ════════════════════════════════════════
void startPlayback(const AudioTask& task) {
    if (!s_downloadQueue) {
        Serial.println("[PLAY] Queue not initialized!");
        return;
    }
    char url[MAX_URL_LEN];
    task.voice_url.toCharArray(url, MAX_URL_LEN);
    if (xQueueSend(s_downloadQueue, url, 0) != pdTRUE) {
        Serial.printf("[PLAY] Download queue full; dropped: %s\n", url);
        return;
    }
    setFaceExpression(FACE_THINKING);
    Serial.printf("[PLAY] Queued for download: %s\n", url);
}

// ════════════════════════════════════════
//  ダウンロード完了チェック → Speaker起動
//  loop()から毎回呼ぶ（Core 1でSpeaker操作するために分離）
// ════════════════════════════════════════
static bool startDownloadedWavPlayback(uint8_t* wavData, size_t wavSize) {
    WavInfo wavInfo;
    if (!parseWavInfo(wavData, wavSize, &wavInfo)) {
        Serial.println("[PLAY] Refusing invalid WAV");
        free(wavData);
        setFaceExpression(FACE_IDLE);
        return false;
    }

    retireCurrentPlaybackBuffer();
    currentWavData = wavData;
    currentWavSize = wavSize;
    currentPcmOffset = wavInfo.dataOffset;
    currentPcmSize = wavInfo.dataSize;
    currentSampleRate = wavInfo.sampleRate;
    currentBytesPerFrame = (wavInfo.channels * wavInfo.bitsPerSample) / 8;

    // 再生時間 + 2秒のデッドライン
    const float bytes_per_sec = (float)currentSampleRate * (float)currentBytesPerFrame;
    playbackDeadlineMs = millis() +
        (unsigned long)((currentPcmSize / bytes_per_sec) * 1000.0f) + 2000;

    // マイク停止 → スピーカー起動
    if (M5.Mic.isRunning()) {
        M5.Mic.end();
        vTaskDelay(pdMS_TO_TICKS(200));  // 固定200ms待機に戻す
    }
    if (!M5.Speaker.isRunning()) {
        M5.Speaker.begin();
    }

    Serial.println("[PLAY] Mic stopped");
    M5.Speaker.setVolume(SPEAKER_VOLUME);
    bool ok = M5.Speaker.playWav(currentWavData, currentWavSize);
    if (!ok) {
        Serial.println("[PLAY] Speaker rejected playWav");
        retireCurrentPlaybackBuffer();
        setFaceExpression(FACE_IDLE);
        micResumeRequested = true;
        return false;
    }
    setFaceExpression(FACE_PLAYING);

    lipSyncOffset = currentPcmOffset;
    lastLipMs     = 0;
    isPlaying     = true;
    s_currentPlaybackIsPcm = false;
    s_currentPcmSessionId = "";
    s_currentPcmFinalSegment = false;
    clearQueuedPcmPlayback();
    playbackStartMs  = millis();
    Serial.println("[PLAY] Speaker started");
    return true;
}

void checkPendingPlayback() {
    if (isPlaying || !s_downloadCompleteQueue) {
        return;
    }

    DownloadedAudio completed = {};
    if (xQueueReceive(s_downloadCompleteQueue, &completed, 0) != pdTRUE) {
        return;
    }
    startDownloadedWavPlayback(completed.data, completed.size);
}

PcmPlaybackResult startPcmPlayback(uint8_t* pcmData, size_t pcmSize, const String& sessionId, bool finalSegment) {
    if (!pcmData || pcmSize == 0) {
        Serial.println("[PCM] Empty body");
        return PCM_PLAYBACK_INVALID;
    }
    if (sessionId.length() == 0) {
        Serial.println("[PCM] Missing session id");
        return PCM_PLAYBACK_INVALID;
    }
    if ((pcmSize % PCM_BYTES_PER_SAMPLE) != 0 || pcmSize > MAX_PCM_BYTES) {
        Serial.printf("[PCM] Invalid size: %u\n", (unsigned)pcmSize);
        return PCM_PLAYBACK_INVALID;
    }
    if (isPlaying || M5.Speaker.isPlaying()) {
        if (s_currentPlaybackIsPcm && sessionId == s_currentPcmSessionId &&
            enqueuePcmBuffer(pcmData, pcmSize, sessionId, finalSegment)) {
            return PCM_PLAYBACK_QUEUED;
        }
        Serial.printf("[PCM] Busy; refusing segment session=%s current=%s\n",
                      sessionId.c_str(), s_currentPcmSessionId.c_str());
        return PCM_PLAYBACK_BUSY;
    }

    if (!s_pcmQueue.empty()) {
        clearQueuedPcmPlayback();
    }

    retireCurrentPlaybackBuffer();

    currentWavData = pcmData;
    currentWavSize = pcmSize;
    currentPcmOffset = 0;
    currentPcmSize = pcmSize;
    currentSampleRate = PCM_SAMPLE_RATE;
    currentBytesPerFrame = PCM_BYTES_PER_SAMPLE;

    const float bytes_per_sec = (float)PCM_SAMPLE_RATE * (float)PCM_BYTES_PER_SAMPLE;
    playbackDeadlineMs = millis() +
        (unsigned long)((pcmSize / bytes_per_sec) * 1000.0f) + 2000;

    if (M5.Mic.isRunning()) {
        M5.Mic.end();
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    if (!M5.Speaker.isRunning()) {
        M5.Speaker.begin();
    }

    M5.Speaker.setVolume(SPEAKER_VOLUME);
    bool ok = M5.Speaker.playRaw((const int16_t*)currentWavData,
                                 currentWavSize / sizeof(int16_t),
                                 PCM_SAMPLE_RATE,
                                 false,
                                 1,
                                 -1,
                                 true);
    if (!ok) {
        Serial.println("[PCM] Speaker rejected playRaw");
        free(currentWavData);
        currentWavData = nullptr;
        currentWavSize = 0;
        currentPcmSize = 0;
        setFaceExpression(FACE_IDLE);
        micResumeRequested = true;
        return PCM_PLAYBACK_SPEAKER_FAILED;
    }

    setFaceExpression(FACE_PLAYING);
    lipSyncOffset = 0;
    lastLipMs = 0;
    isPlaying = true;
    s_currentPlaybackIsPcm = true;
    s_currentPcmSessionId = sessionId;
    s_currentPcmFinalSegment = finalSegment;
    playbackStartMs = millis();
    Serial.printf("[PCM] Speaker started: session=%s bytes=%u final=%s queue=%u @ 24kHz mono s16le\n",
                  sessionId.c_str(), (unsigned)pcmSize,
                  finalSegment ? "true" : "false", (unsigned)s_pcmQueuedBytes);
    return PCM_PLAYBACK_OK;
}

// ════════════════════════════════════════
//  口パク更新（loop()から毎回呼ぶ）
// ════════════════════════════════════════
void updateLipSync() {
    if (!isPlaying || currentWavData == nullptr || currentWavSize == 0) return;

    unsigned long now = millis();
    if (now - lastLipMs < LIPSYNC_INTERVAL_MS) return;
    lastLipMs = now;

    if (lipSyncOffset < currentPcmOffset) lipSyncOffset = currentPcmOffset;
    if (lipSyncOffset >= currentPcmOffset + currentPcmSize) {
        setMouthOpen(0.0f);
        return;
    }

    int16_t* pcm = (int16_t*)(currentWavData + lipSyncOffset);
    size_t remainBytes = currentPcmOffset + currentPcmSize - lipSyncOffset;
    size_t samples = min((size_t)LIPSYNC_CHUNK_SAMPLES, remainBytes / sizeof(int16_t));
    if (samples == 0) {
        setMouthOpen(0.0f);
        return;
    }

    float sum = 0.0f;
    for (size_t i = 0; i < samples; i++) {
        float v = (float)pcm[i] / 32768.0f;
        sum += v * v;
    }
    setMouthOpen(constrain(sqrtf(sum / samples) * 8.0f, 0.0f, 1.0f));
    lipSyncOffset += samples * sizeof(int16_t);
}

PlaybackStatus getPlaybackStatus() {
    PlaybackStatus status;
    status.playing = isPlaying;
    status.pcm = s_currentPlaybackIsPcm;
    status.pcmFinalSegment = s_currentPcmFinalSegment;
    status.pcmSession = s_currentPcmSessionId.c_str();
    status.currentBytes = currentWavSize;
    status.queuedPcmBytes = s_pcmQueuedBytes;
    status.queuedPcmSegments = s_pcmQueue.size();
    status.startedMs = playbackStartMs;
    status.deadlineMs = playbackDeadlineMs;
    return status;
}

// ════════════════════════════════════════
//  音声ダウンロード（Core 0のタスクから呼ぶ）
// ════════════════════════════════════════
bool downloadVoice(const String& url, uint8_t** outData, size_t* outSize) {
    HTTPClient http;
    Serial.printf("[DOWNLOAD] URL: %s\n", url.c_str());

    *outData = nullptr;
    *outSize = 0;

    http.begin(url);
    http.setTimeout(DOWNLOAD_TIMEOUT_MS);
    int httpCode = http.GET();

    if (httpCode != HTTP_CODE_OK) {
        Serial.printf("[DOWNLOAD] HTTP error: %d\n", httpCode);
        http.end();
        return false;
    }

    int len = http.getSize();
    if (len <= 0 || len > MAX_WAV_BYTES) {
        Serial.printf("[DOWNLOAD] Invalid content length: %d\n", len);
        http.end();
        return false;
    }

    uint8_t* wavData = (uint8_t*)ps_malloc(len);
    if (!wavData) {
        Serial.println("[DOWNLOAD] ps_malloc failed");
        http.end();
        return false;
    }

    WiFiClient* stream = http.getStreamPtr();
    size_t bytesRead = 0;
    unsigned long lastProgressMs = millis();
    while (bytesRead < (size_t)len) {
        size_t available = stream->available();
        if (available) {
            size_t toRead = min(available, (size_t)(len - bytesRead));
            size_t got = stream->readBytes(wavData + bytesRead, toRead);
            if (got == 0) {
                Serial.println("[DOWNLOAD] Read returned 0 bytes");
                break;
            }
            bytesRead += got;
            lastProgressMs = millis();
        } else if (!http.connected()) {
            Serial.println("[DOWNLOAD] Connection closed before full read");
            break;
        } else if (millis() - lastProgressMs > DOWNLOAD_TIMEOUT_MS) {
            Serial.println("[DOWNLOAD] Read timeout");
            break;
        }
        delay(1);
    }
    http.end();

    if (bytesRead != (size_t)len) {
        Serial.printf("[DOWNLOAD] Incomplete read: got=%u expected=%u\n",
                      (unsigned)bytesRead, (unsigned)len);
        free(wavData);
        return false;
    }

    WavInfo wavInfo;
    if (!parseWavInfo(wavData, (size_t)len, &wavInfo)) {
        free(wavData);
        return false;
    }

    Serial.printf("[DOWNLOAD] Complete: bytes=%u data=%u offset=%u\n",
                  (unsigned)len, (unsigned)wavInfo.dataSize,
                  (unsigned)wavInfo.dataOffset);
    *outData = wavData;
    *outSize = (size_t)len;
    return true;
}

// ════════════════════════════════════════
//  再生完了後の次キュー処理
// ════════════════════════════════════════
void processAudioQueue() {
    if (isPlaying) return;

    setMouthOpen(0.0f);

    if (!s_pcmQueue.empty()) {
        PcmBuffer nextPcm = s_pcmQueue.front();
        s_pcmQueue.pop();
        s_pcmQueuedBytes -= nextPcm.size;

        PcmPlaybackResult result = startPcmPlayback(
            nextPcm.data,
            nextPcm.size,
            nextPcm.sessionId,
            nextPcm.finalSegment
        );
        if (result != PCM_PLAYBACK_OK) {
            if (result != PCM_PLAYBACK_SPEAKER_FAILED) {
                free(nextPcm.data);
            }
            Serial.printf("[PCM] Dropped queued segment: result=%d\n", result);
        }
        if (isPlaying) {
            return;
        }
    }

    checkPendingPlayback();
    if (isPlaying) {
        return;
    }

    if (audioQueue.empty()) {
        if (s_currentPlaybackIsPcm && s_currentPcmFinalSegment) {
            Serial.printf("[PCM] Session complete: %s\n", s_currentPcmSessionId.c_str());
        }
        s_currentPlaybackIsPcm = false;
        s_currentPcmSessionId = "";
        s_currentPcmFinalSegment = false;
        setFaceExpression(FACE_IDLE);
        return;
    }

    AudioTask next = audioQueue.top();
    audioQueue.pop();
    startPlayback(next);
    last_played_voice_id = next.voice_id;
}
