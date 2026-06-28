#include "audio_gate.h"

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <string.h>

static constexpr size_t OWNER_LEN = 32;

static SemaphoreHandle_t s_audioMutex = nullptr;
static char s_owner[OWNER_LEN] = "none";
static uint32_t s_lockCount = 0;
static uint32_t s_failedAcquireCount = 0;

void initAudioGate() {
    if (s_audioMutex) {
        return;
    }
    s_audioMutex = xSemaphoreCreateRecursiveMutex();
    if (!s_audioMutex) {
        Serial.println("[AUDIO] Failed to create audio gate");
        return;
    }
    Serial.println("[AUDIO] Gate ready");
    logAudioMemory("gate-init");
}

bool audioGateEnter(const char* owner, uint32_t timeoutMs) {
    if (!s_audioMutex) {
        initAudioGate();
    }
    if (!s_audioMutex) {
        s_failedAcquireCount++;
        return false;
    }

    TickType_t ticks = timeoutMs == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeoutMs);
    if (xSemaphoreTakeRecursive(s_audioMutex, ticks) == pdTRUE) {
        strncpy(s_owner, owner ? owner : "unknown", OWNER_LEN - 1);
        s_owner[OWNER_LEN - 1] = '\0';
        s_lockCount++;
        return true;
    }

    s_failedAcquireCount++;
    Serial.printf("[AUDIO] Gate busy: requested=%s owner=%s failed=%u\n",
                  owner ? owner : "unknown", s_owner, (unsigned)s_failedAcquireCount);
    return false;
}

void audioGateLeave(const char* owner) {
    if (!s_audioMutex) {
        return;
    }
    strncpy(s_owner, "none", OWNER_LEN - 1);
    s_owner[OWNER_LEN - 1] = '\0';
    if (xSemaphoreGiveRecursive(s_audioMutex) != pdTRUE) {
        Serial.printf("[AUDIO] Gate leave failed: owner=%s\n", owner ? owner : "unknown");
    }
}

AudioGateStatus getAudioGateStatus() {
    AudioGateStatus status;
    status.initialized = s_audioMutex != nullptr;
    status.locked = strcmp(s_owner, "none") != 0;
    status.owner = s_owner;
    status.lockCount = s_lockCount;
    status.failedAcquireCount = s_failedAcquireCount;
    status.freeHeap = ESP.getFreeHeap();
    status.freePsram = ESP.getFreePsram();
    status.stackWatermark = (uint32_t)uxTaskGetStackHighWaterMark(nullptr);
    return status;
}

void logAudioMemory(const char* label) {
    AudioGateStatus status = getAudioGateStatus();
    Serial.printf("[AUDIO] %s heap=%u psram=%u stack_watermark=%u owner=%s locks=%u failed=%u\n",
                  label ? label : "-",
                  (unsigned)status.freeHeap,
                  (unsigned)status.freePsram,
                  (unsigned)status.stackWatermark,
                  status.owner,
                  (unsigned)status.lockCount,
                  (unsigned)status.failedAcquireCount);
}
