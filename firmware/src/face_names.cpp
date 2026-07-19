#include "face_names.h"

#include <string.h>

static const int NUM_FACES = 10;

const char* whaleFaceName(WhaleFace face) {
    switch (face) {
        case WHALE_CALM:      return "calm";
        case WHALE_HAPPY:     return "happy";
        case WHALE_SAD:       return "sad";
        case WHALE_SLEEP:     return "sleep";
        case WHALE_SURPRISED: return "surprised";
        case WHALE_SHY:       return "shy";
        case WHALE_SMUG:      return "smug";
        case WHALE_KISS:      return "kiss";
        case WHALE_ANGRY:     return "angry";
        case WHALE_ANXIOUS:   return "anxious";
        default:              return "unknown";
    }
}

// 旧名 → 新脸（桥和历史脚本发的命令不至于失效）
static const struct { const char* alias; WhaleFace face; } FACE_ALIASES[] = {
    {"thinking", WHALE_CALM},
    {"sleepy",   WHALE_SLEEP},
    {"pouty",    WHALE_ANGRY},
};

bool whaleFaceFromName(const char* name, WhaleFace* face) {
    if (!name || !face) {
        return false;
    }
    for (int i = 0; i < NUM_FACES; i++) {
        WhaleFace candidate = (WhaleFace)i;
        if (strcmp(name, whaleFaceName(candidate)) == 0) {
            *face = candidate;
            return true;
        }
    }
    for (auto& a : FACE_ALIASES) {
        if (strcmp(name, a.alias) == 0) {
            *face = a.face;
            return true;
        }
    }
    return false;
}
