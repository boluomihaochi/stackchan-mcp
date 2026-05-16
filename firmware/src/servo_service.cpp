#include "servo_service.h"
#include "drivers/SCServo/SCSCL.h"
#include <Arduino.h>

// ── Servo hardware config ──────────────────────────
#define SERVO_UART      UART_NUM_1
#define SERVO_BAUD      1000000
#define SERVO_TX_PIN    6    // UART1 TX (StackChan-BSP standard)
#define SERVO_RX_PIN    7    // UART1 RX (StackChan-BSP standard)

#define YAW_ID          1       // Servo ID for left/right
#define PITCH_ID        2       // Servo ID for up/down

// Raw position calibration (from StackChan-BSP defaults)
#define YAW_ZERO_POS    460
#define PITCH_ZERO_POS  620

// Angle limits (degrees)
#define YAW_MIN_DEG    -128.0f
#define YAW_MAX_DEG     128.0f
#define PITCH_MIN_DEG   0.0f
#define PITCH_MAX_DEG   90.0f

// Raw position limits
#define RAW_POS_MIN     0
#define RAW_POS_MAX     1000

static SCSCL scs;
static bool servoReady = false;
static bool lastCommandOk = false;
static int lastYawRaw = -1;
static int lastPitchRaw = -1;
static int lastYawResult = 0;
static int lastPitchResult = 0;
static unsigned long lastCommandMs = 0;

bool isServoReady() { return servoReady; }

// ── Helpers ────────────────────────────────────────

// Convert degrees to raw servo position
// Formula from BSP: raw = zero_pos + (angle_tenth_deg * 16 / 5 / 10)
//   angle_tenth_deg = degrees * 10
//   so raw = zero_pos + degrees * 10 * 16 / 50 = zero_pos + degrees * 3.2
static int degToRaw(float degrees, int zeroPos) {
    int raw = zeroPos + (int)(degrees * 3.2f);
    if (raw < RAW_POS_MIN) raw = RAW_POS_MIN;
    if (raw > RAW_POS_MAX) raw = RAW_POS_MAX;
    return raw;
}

// Map speed percentage (0-100) to movement time in ms
// Higher speed = lower time
static uint16_t speedToTime(int speedPct) {
    if (speedPct < 0) speedPct = 0;
    if (speedPct > 100) speedPct = 100;
    // speed 100 -> 80ms, speed 0 -> 2000ms
    return (uint16_t)(2000 - speedPct * 19);
}

static float clampf(float v, float lo, float hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

static bool writePoseRaw(int yawRaw, int pitchRaw, uint16_t timeMs) {
    u8 ids[2] = {YAW_ID, PITCH_ID};
    u16 positions[2] = {(u16)yawRaw, (u16)pitchRaw};
    u16 times[2] = {timeMs, timeMs};
    u16 speeds[2] = {0, 0};

    lastYawRaw = yawRaw;
    lastPitchRaw = pitchRaw;
    lastCommandMs = millis();

    // Stage both writes with ACK, then broadcast the action so both axes start together.
    lastYawResult = scs.RegWritePos(YAW_ID, positions[0], times[0], speeds[0]);
    lastPitchResult = scs.RegWritePos(PITCH_ID, positions[1], times[1], speeds[1]);
    if (lastYawResult && lastPitchResult) {
        scs.RegWriteAction(0xfe);
    } else {
        // Fall back to sync write if ACKs are unreliable on the local bus.
        scs.SyncWritePos(ids, 2, positions, times, speeds);
    }

    lastCommandOk = (lastYawResult != 0 && lastPitchResult != 0);
    return lastCommandOk;
}

static ServoFeedback readFeedback(u8 id) {
    ServoFeedback fb;
    fb.ok = (scs.FeedBack(id) > 0);
    if (fb.ok) {
        fb.position = scs.ReadPos(-1);
        fb.speed = scs.ReadSpeed(-1);
        fb.load = scs.ReadLoad(-1);
        fb.voltage = scs.ReadVoltage(-1);
        fb.temperature = scs.ReadTemper(-1);
        fb.moving = scs.ReadMove(-1);
        fb.current = scs.ReadCurrent(-1);
        return fb;
    }

    fb.position = scs.ReadPos(id);
    fb.speed = scs.ReadSpeed(id);
    fb.load = scs.ReadLoad(id);
    fb.voltage = scs.ReadVoltage(id);
    fb.temperature = scs.ReadTemper(id);
    fb.moving = scs.ReadMove(id);
    fb.current = scs.ReadCurrent(id);
    fb.ok = (fb.position >= 0 || fb.voltage >= 0);
    return fb;
}

// ── Public API ─────────────────────────────────────

bool initServo() {
    if (!scs.begin(SERVO_UART, SERVO_BAUD, SERVO_TX_PIN, SERVO_RX_PIN)) {
        Serial.println("[SERVO] UART init failed");
        return false;
    }

    // Enable torque on both servos
    int yawTorque = scs.EnableTorque(YAW_ID, 1);
    int pitchTorque = scs.EnableTorque(PITCH_ID, 1);

    // Move to home position gently
    int yawHome = degToRaw(0, YAW_ZERO_POS);
    int pitchHome = degToRaw(0, PITCH_ZERO_POS);
    writePoseRaw(yawHome, pitchHome, 1000);

    servoReady = true;
    Serial.printf("[SERVO] Ready (yaw ID=%d torque=%d, pitch ID=%d torque=%d, UART1 TX=%d RX=%d, cmd=%s)\n",
                  YAW_ID, yawTorque, PITCH_ID, pitchTorque, SERVO_TX_PIN, SERVO_RX_PIN,
                  lastCommandOk ? "OK" : "NO_ACK");
    return true;
}

bool servoMove(float yawDeg, float pitchDeg, int speedPct) {
    if (!servoReady) return false;

    yawDeg = clampf(yawDeg, YAW_MIN_DEG, YAW_MAX_DEG);
    pitchDeg = clampf(pitchDeg, PITCH_MIN_DEG, PITCH_MAX_DEG);

    int yawRaw = degToRaw(yawDeg, YAW_ZERO_POS);
    int pitchRaw = degToRaw(pitchDeg, PITCH_ZERO_POS);
    uint16_t timeMs = speedToTime(speedPct);

    bool ok = writePoseRaw(yawRaw, pitchRaw, timeMs);

    Serial.printf("[SERVO] Move yaw=%.1f° pitch=%.1f° speed=%d%% (raw: %d,%d time: %dms ack: %d,%d %s)\n",
                  yawDeg, pitchDeg, speedPct, yawRaw, pitchRaw, timeMs,
                  lastYawResult, lastPitchResult, ok ? "OK" : "NO_ACK");
    return ok;
}

bool servoHome(int speedPct) {
    bool ok = servoMove(0, 0, speedPct);
    Serial.println("[SERVO] Home");
    return ok;
}

bool servoNod() {
    if (!servoReady) return false;
    Serial.println("[SERVO] Nod");

    int pitchHome = degToRaw(0, PITCH_ZERO_POS);
    int pitchUp = degToRaw(30, PITCH_ZERO_POS);
    int pitchDown = degToRaw(-10, PITCH_ZERO_POS);  // slight down
    bool ok = true;

    // Up
    ok = (scs.WritePos(PITCH_ID, pitchUp, 250, 0) != 0) && ok;
    delay(300);
    // Down
    ok = (scs.WritePos(PITCH_ID, pitchDown, 200, 0) != 0) && ok;
    delay(250);
    // Up again
    ok = (scs.WritePos(PITCH_ID, pitchUp, 250, 0) != 0) && ok;
    delay(300);
    // Return home
    ok = (scs.WritePos(PITCH_ID, pitchHome, 400, 0) != 0) && ok;
    lastPitchRaw = pitchHome;
    lastCommandOk = ok;
    lastCommandMs = millis();
    Serial.printf("[SERVO] Nod result=%s\n", ok ? "OK" : "NO_ACK");
    return ok;
}

bool servoShake() {
    if (!servoReady) return false;
    Serial.println("[SERVO] Shake");

    int yawHome = degToRaw(0, YAW_ZERO_POS);
    int yawLeft = degToRaw(-40, YAW_ZERO_POS);
    int yawRight = degToRaw(40, YAW_ZERO_POS);
    bool ok = true;

    // Left
    ok = (scs.WritePos(YAW_ID, yawLeft, 250, 0) != 0) && ok;
    delay(300);
    // Right
    ok = (scs.WritePos(YAW_ID, yawRight, 250, 0) != 0) && ok;
    delay(300);
    // Left again
    ok = (scs.WritePos(YAW_ID, yawLeft, 250, 0) != 0) && ok;
    delay(300);
    // Return home
    ok = (scs.WritePos(YAW_ID, yawHome, 400, 0) != 0) && ok;
    lastYawRaw = yawHome;
    lastCommandOk = ok;
    lastCommandMs = millis();
    Serial.printf("[SERVO] Shake result=%s\n", ok ? "OK" : "NO_ACK");
    return ok;
}

ServoStatus getServoStatus() {
    ServoStatus status;
    status.ready = servoReady;
    status.lastCommandOk = lastCommandOk;
    status.lastYawRaw = lastYawRaw;
    status.lastPitchRaw = lastPitchRaw;
    status.lastYawResult = lastYawResult;
    status.lastPitchResult = lastPitchResult;
    status.lastCommandMs = lastCommandMs;
    if (servoReady) {
        status.yaw = readFeedback(YAW_ID);
        status.pitch = readFeedback(PITCH_ID);
    }
    return status;
}
