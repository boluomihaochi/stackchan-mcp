#pragma once

// Whale face names (for HTTP/MCP direct control)
// v2 (2026-07-19): 闲鱼画师10表情版
enum WhaleFace {
    WHALE_CALM      = 0,
    WHALE_HAPPY     = 1,
    WHALE_SAD       = 2,
    WHALE_SLEEP     = 3,
    WHALE_SURPRISED = 4,
    WHALE_SHY       = 5,
    WHALE_SMUG      = 6,
    WHALE_KISS      = 7,
    WHALE_ANGRY     = 8,
    WHALE_ANXIOUS   = 9,
    // 旧名别名——固件内部状态机和桥的旧命令还在用
    WHALE_THINKING  = WHALE_CALM,
    WHALE_SLEEPY    = WHALE_SLEEP,
    WHALE_POUTY     = WHALE_ANGRY,
};

const char* whaleFaceName(WhaleFace face);
bool whaleFaceFromName(const char* name, WhaleFace* face);
