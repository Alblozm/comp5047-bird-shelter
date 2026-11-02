// Laptop tests for climate/include/ShelterLogic.h (perch detection, mist limits).
//   c++ -std=c++17 -Iclimate/include climate/test_host/test_logic.cpp -o /tmp/test_logic && /tmp/test_logic
#include <cstdio>
#include <cstdlib>
#include "ShelterLogic.h"

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { std::printf("FAIL line %d: %s\n", __LINE__, #cond); ++failures; } } while (0)

using P = PerchDetector;
constexpr float JUMP = 300, ON = 600, OFF = 300;

// Feed a constant reading for `ms` in 10 ms steps; returns the first event seen.
static P::Event feed(P &p, uint32_t &now, int raw, uint32_t ms, bool sudden = true) {
  P::Event first = P::NONE;
  for (uint32_t end = now + ms; now < end; now += 10) {
    P::Event e = p.update(raw, now, sudden, JUMP, ON, OFF);
    if (first == P::NONE) first = e;
  }
  return first;
}

static void perchSuddenPress() {
  P p; uint32_t now = 1000;
  CHECK(feed(p, now, 500, 5000) == P::NONE);
  CHECK(feed(p, now, 900, 100) == P::NONE);        // not held long enough yet
  CHECK(feed(p, now, 900, 100) == P::ARRIVED);
  CHECK(p.perch);
  CHECK(feed(p, now, 700, 1000) == P::NONE);       // still well above the usual reading
  CHECK(feed(p, now, 520, 100) == P::LEFT);
  CHECK(!p.perch);
}

static void perchIgnoresSpike() {
  P p; uint32_t now = 1000;
  feed(p, now, 500, 2000);
  CHECK(feed(p, now, 1500, 100) == P::NONE);
  CHECK(feed(p, now, 500, 1000) == P::NONE);
  CHECK(!p.perch);
}

static void perchIgnoresSlowDrift() {
  P p; uint32_t now = 1000;
  bool arrived = false;
  for (int raw = 500; raw <= 1500; ++raw)          // +1000 over 10 minutes
    for (int i = 0; i < 60; ++i, now += 10) arrived |= p.update(raw, now, true, JUMP, ON, OFF) == P::ARRIVED;
  CHECK(!arrived);
  CHECK(p.base > 1400);
  CHECK(feed(p, now, 1900, 300) == P::ARRIVED);    // a real landing on top of the drift still counts
}

static void perchSettlesAfterLongLoad() {
  P p; uint32_t now = 1000;
  feed(p, now, 500, 2000);
  CHECK(feed(p, now, 1200, 300) == P::ARRIVED);
  bool settled = false;
  for (uint32_t end = now + P::SETTLE_MS + 1000; now < end; now += 10)
    settled |= p.update(1200, now, true, JUMP, ON, OFF) == P::SETTLED;
  CHECK(settled);
  CHECK(!p.perch);
  CHECK(p.base > 1150);
  CHECK(feed(p, now, 1600, 300) == P::ARRIVED);    // a bird landing on the leftover load
}

static void perchBaselineFallsFast() {
  P p; uint32_t now = 1000;
  feed(p, now, 900, 5000);
  feed(p, now, 500, 5000);                         // load removed while not perched
  CHECK(p.base < 520);
  CHECK(feed(p, now, 850, 300) == P::ARRIVED);
}

static void perchFixedThresholds() {
  P p; uint32_t now = 1000;
  CHECK(feed(p, now, 400, 1000, false) == P::NONE);
  CHECK(feed(p, now, 650, 300, false) == P::ARRIVED);
  CHECK(feed(p, now, 650, P::SETTLE_MS + 1000, false) == P::NONE);   // no settling in fixed mode
  CHECK(feed(p, now, 250, 100, false) == P::LEFT);
}

static void budgetRollsOverAnHour() {
  MistBudget b;
  b.add(0, 0);
  b.add(10000, 30000);
  CHECK(b.usedMs() == 30000);
  b.add(30 * 60000, 20000);
  CHECK(b.usedMs() == 50000);
  b.add(61 * 60000, 0);                            // the first spray is now over an hour old
  CHECK(b.usedMs() == 20000);
  b.add(200 * 60000, 0);
  CHECK(b.usedMs() == 0);
}

static void budgetSurvivesMillisWrap() {
  MistBudget b;
  b.add(0xFFFFFFFFu - 5000, 4000);
  CHECK(b.usedMs() == 4000);
  b.add(1000, 1000);                               // millis() wrapped: old minutes are dropped
  CHECK(b.usedMs() <= 5000);
}

// Worst case: something asks for an endless spray and re-asks the moment it stops.
// Measures the longest continuous on-time and the total in any hour.
struct Run { uint32_t longest = 0, total = 0, sprays = 0; };
static Run hammer(uint32_t limit, uint32_t capMs, uint32_t hours, bool waterAt = true) {
  MistGuard g; Run r;
  bool requested = false; uint32_t start = 0, onFor = 0;
  for (uint32_t now = 0; now < hours * 3600000u; now += 20) {
    if (!requested && !g.resting) { requested = true; start = now; }
    bool allow = g.allow(now, requested, start, limit, capMs, waterAt);
    if (!allow) requested = false;
    if (allow) { onFor += 20; r.total += 20; if (onFor > r.longest) r.longest = onFor; }
    else { if (onFor) ++r.sprays; onFor = 0; }
  }
  return r;
}

static void guardCapsEveryCase() {
  Run endless = hammer(0x7FFFFFFF, 300000, 1);
  CHECK(endless.longest <= MistBudget::MAX_MS);
  CHECK(endless.total <= 300000);
  CHECK(endless.total >= 290000);                  // the cap is what stops it, not something else

  Run pulse = hammer(5000, 300000, 1);
  CHECK(pulse.longest <= 5000);
  CHECK(pulse.total <= 300000);

  Run twoHours = hammer(0x7FFFFFFF, 300000, 2);    // budget comes back after an hour, still capped
  CHECK(twoHours.total <= 600000);
  CHECK(twoHours.longest <= MistBudget::MAX_MS);

  Run dry = hammer(5000, 300000, 1, false);
  CHECK(dry.total == 0);
}

static void guardRestsAfterLongSpray() {
  MistGuard g;
  uint32_t now = 0;
  for (; now <= MistBudget::MAX_MS; now += 20) g.allow(now, true, 0, 0x7FFFFFFF, 1800000, true);
  CHECK(g.resting);
  CHECK(!g.allow(now + 1000, true, now + 1000, 2000, 1800000, true));   // new request during the rest
  CHECK(g.allow(now + MistGuard::REST_MS + 20, true, now + MistGuard::REST_MS + 20, 2000, 1800000, true));
}

int main() {
  perchSuddenPress();
  perchIgnoresSpike();
  perchIgnoresSlowDrift();
  perchSettlesAfterLongLoad();
  perchBaselineFallsFast();
  perchFixedThresholds();
  budgetRollsOverAnHour();
  budgetSurvivesMillisWrap();
  guardCapsEveryCase();
  guardRestsAfterLongSpray();
  std::printf(failures ? "%d FAILED\n" : "all passed\n", failures);
  return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
