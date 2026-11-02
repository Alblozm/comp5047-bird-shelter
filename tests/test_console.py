"""Unit tests for app/shelter_console.py (no hardware, no network)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import shelter_console as sc  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_json_inside_text(self):
        r = sc.parse_ai_json('Sure:\n```json\n{"bird": true, "species_en": "noisy miner", "confidence": 0.8}\n```')
        self.assertTrue(r["bird"])
        self.assertEqual(r["species_en"], "noisy miner")
        self.assertEqual(r["species_zh"], "未知")

    def test_confidence_clamped_and_bad_values(self):
        self.assertEqual(sc.parse_ai_json('{"bird": 1, "confidence": 7}')["confidence"], 1.0)
        self.assertEqual(sc.parse_ai_json('{"bird": 0, "confidence": "high"}')["confidence"], 0.0)

    def test_no_json(self):
        with self.assertRaises(ValueError):
            sc.parse_ai_json("I cannot see an image")


class BundleTests(unittest.TestCase):
    def test_round_trip_is_not_plaintext(self):
        cfg = {"OPENAI_API_KEY": "sk-test-123", "OPENAI_BASE_URL": "https://x/v1"}
        text = sc.encode_bundle(cfg)
        self.assertNotIn("sk-test-123", text)
        self.assertEqual(sc.decode_bundle(text), cfg)


class VisitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.v = sc.Visits(Path(self.tmp.name) / "visits.json")
        self.env = {"t": 30, "rh": 50, "lux": 500}

    def tearDown(self):
        self.tmp.cleanup()

    def test_detections_within_gap_merge(self):
        self.v.seen(1000, 60, "彩虹吸蜜鹦鹉", .9, "ai", "a.jpg", self.env, "A")
        self.v.seen(1030, 60, "彩虹吸蜜鹦鹉", .95, "ai", "b.jpg", self.env, "A")
        self.assertEqual(len(self.v.items), 1)
        self.assertEqual(self.v.items[0]["detections"], 2)
        self.assertEqual(self.v.items[0]["end"], 1030)

    def test_gap_or_new_location_starts_new_visit(self):
        self.v.seen(1000, 60, "x", .9, "ai", None, self.env, "A")
        self.v.seen(1100, 60, "x", .9, "ai", None, self.env, "A")
        self.v.seen(1110, 60, "x", .9, "ai", None, self.env, "B")
        self.assertEqual(len(self.v.items), 3)

    def test_leave_and_mist(self):
        self.v.seen(1000, 60, "x", .9, "ai", None, self.env, "A")
        self.v.left(1040)
        self.v.misted(1050, 60)
        self.assertEqual(self.v.items[0]["end"], 1040)
        self.assertTrue(self.v.items[0]["misted"])
        reloaded = sc.Visits(self.v.path)
        self.assertEqual(reloaded.items[0]["end"], 1040)


class HostSettingTests(unittest.TestCase):
    def test_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc.DATA = Path(tmp)
            c = sc.Console()
            c.save_host({"confidence": 3, "visit_gap_s": 1, "ai_fail": "bogus", "language": "fr",
                         "location": "  ", "ai_model": "gemini 2.5;rm", "unknown": 1})
            self.assertEqual(c.host["confidence"], 1.0)
            self.assertEqual(c.host["visit_gap_s"], 5)
            self.assertEqual(c.host["ai_fail"], "perch")
            self.assertEqual(c.host["language"], "zh")
            self.assertEqual(c.host["location"], "未命名地点")
            self.assertEqual(c.host["ai_model"], "gemini2.5rm")
            self.assertNotIn("unknown", c.host)


class PerchAndMistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sc.DATA = Path(self.tmp.name)
        self.c = sc.Console()
        self.sent = []
        self.c.send = lambda role, line: self.sent.append(line)
        self.c.connection["climate"]["connected"] = True
        self.c.received["climate"] = sc.time.time()
        self.c.latest["climate"] = {"fsr": 420}

    def tearDown(self):
        self.tmp.cleanup()

    def test_calibrate_sudden_mode_rebases(self):
        self.c.board_cfg = {"perch_mode": 1}
        self.assertEqual(self.c.calibrate_perch(), (420, "sudden"))
        self.assertEqual(self.sent, ["REBASE"])

    def test_calibrate_fixed_mode_sets_thresholds(self):
        self.c.board_cfg = {"perch_mode": 0}
        self.assertEqual(self.c.calibrate_perch(), (420, "fixed"))
        self.assertEqual(sorted(self.sent), ["SET fsr_off 620", "SET fsr_on 920"])

    def test_simulated_mist_caps(self):
        sim = sc.SimClimate(self.c)
        now = sc.time.time()
        self.assertTrue(sim.start_mist(now, 120, "cooling"))
        self.assertLessEqual(sim.mist_until - now, sc.MIST_MAX_S)
        sim.cfg["mist_hour_s"] = 10
        sim.sprayed.extend([now - 1] * 95)   # 9.5 s already sprayed this hour
        sim.mist = False
        self.assertFalse(sim.start_mist(now, 5, "manual"))


if __name__ == "__main__":
    unittest.main()
