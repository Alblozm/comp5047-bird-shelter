// Decision logic for shelter.cpp with no Arduino calls, so it can be tested on
// the laptop:  c++ -std=c++17 -Iclimate/include climate/test_host/test_logic.cpp
#pragma once
#include <stdint.h>

// Perch: a bird landing is a sudden rise above the usual reading. The usual
// reading (baseline) follows slow changes such as the perch settling or
// temperature drift, so only a quick press counts as an arrival.
struct PerchDetector {
  enum Event { NONE, ARRIVED, LEFT, SETTLED };
  static constexpr uint32_t HOLD_MS = 150;                  // rise must last this long (ignores spikes)
  static constexpr uint32_t SETTLE_MS = 10UL * 60 * 1000;   // load that never leaves becomes the new normal
  static constexpr float RISE_TAU_S = 20, FALL_TAU_S = 1;   // baseline drifts up slowly, down quickly

  bool perch = false;
  float base = -1;   // < 0 until the first reading
  bool rising = false;
  uint32_t riseSince = 0, perchSince = 0, last = 0;

  void rebase(int raw) { base = raw; perch = rising = false; }

  // sudden: arrival = rise of `jump` above the baseline; otherwise fixed thresholds on/off.
  Event update(int raw, uint32_t now, bool sudden, float jump, float on, float off) {
    float dt = base < 0 ? 0 : (now - last) / 1000.0f;
    last = now;
    if (base < 0) base = raw;
    float rise = raw - base;
    if (!perch) {
      if (sudden ? rise >= jump : raw > on) {
        if (!rising) { rising = true; riseSince = now; }
        if (now - riseSince < HOLD_MS) return NONE;
        perch = true; rising = false; perchSince = now;
        return ARRIVED;
      }
      rising = false;
      if (rise < jump) {
        float k = dt / (rise > 0 ? RISE_TAU_S : FALL_TAU_S);
        base += rise * (k < 1 ? k : 1);
      }
      return NONE;
    }
    if (sudden ? rise < jump / 2 : raw < off) { perch = false; return LEFT; }
    if (sudden && now - perchSince >= SETTLE_MS) { rebase(raw); return SETTLED; }
    return NONE;
  }
};

// Every spray ends: one continuous spray is capped at MAX_MS whoever started it,
// and all spraying (cooling, tests, anything) is capped per rolling hour.
struct MistBudget {
  static constexpr uint32_t MAX_MS = 30000;
  uint32_t perMinute[60] = {};
  uint32_t minute = 0;
  bool started = false;

  void add(uint32_t now, uint32_t sprayedMs) {
    uint32_t m = now / 60000;
    if (!started) { minute = m; started = true; }
    for (uint32_t i = 1; i <= m - minute && i <= 60; ++i) perMinute[(minute + i) % 60] = 0;
    minute = m;
    perMinute[m % 60] += sprayedMs;
  }

  uint32_t usedMs() const {
    uint32_t sum = 0;
    for (uint32_t v : perMinute) sum += v;
    return sum;
  }
};

// The safety task's decision every tick: may the mister be on until the next tick?
// The caller drives the pin from the answer, so the pin and this bookkeeping agree.
struct MistGuard {
  static constexpr uint32_t REST_MS = 5000;   // forced pause after a spray reaches MAX_MS
  MistBudget budget;
  bool wasOn = false, resting = false;
  uint32_t onSince = 0, restSince = 0, last = 0;

  // on: a spray is requested; start/limit: that spray's request time and length;
  // capMs: hourly total; waterOk: tank interlock.
  bool allow(uint32_t now, bool on, uint32_t start, uint32_t limit, uint32_t capMs, bool waterOk) {
    budget.add(now, wasOn ? now - last : 0);
    last = now;
    if (resting && now - restSince >= REST_MS) resting = false;
    if (on && !wasOn) onSince = now;
    if (on && wasOn && now - onSince >= MistBudget::MAX_MS) { resting = true; restSince = now; }
    wasOn = on && !resting && (int32_t)(now - start) < (int32_t)limit && budget.usedMs() < capMs && waterOk;
    return wasOn;
  }
};
