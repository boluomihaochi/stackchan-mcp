#pragma once

#include <Arduino.h>

struct AudioGateStatus {
    bool initialized = false;
    bool locked = false;
    const char* owner = "none";
    uint32_t lockCount = 0;
    uint32_t failedAcquireCount = 0;
    uint32_t freeHeap = 0;
    uint32_t freePsram = 0;
    uint32_t stackWatermark = 0;
};

void initAudioGate();
bool audioGateEnter(const char* owner, uint32_t timeoutMs);
void audioGateLeave(const char* owner);
AudioGateStatus getAudioGateStatus();
void logAudioMemory(const char* label);
