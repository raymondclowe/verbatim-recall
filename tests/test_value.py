"""vr_value tests: every placeholder is filled, and the model arithmetic holds.

Run:  python -m unittest discover -s tests -v     (stdlib only, no network, no DB)
"""

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location("vr_value", os.path.join(ROOT, "vr_value.py"))
vv = importlib.util.module_from_spec(spec)
sys.modules["vr_value"] = vv
spec.loader.exec_module(vv)

SESS = "20260101_000000_aaaaaaa"

# One fire with a strict span and a carve-out announce, one fire with a loose span,
# one miss (carries no tokens), one replay row.
ROWS = [
    {"ev": "fire", "at": "2026-11-23T10:00:00", "session": SESS, "tokens": 100, "ms": 50,
     "turns": 500, "terms": ["alpha", "beta"],
     "ptrs": [{"o": SESS, "lo": 100, "hi": 160, "tier": "strict"},
              {"o": SESS, "lo": 200, "hi": 900, "tier": "announce"}]},
    {"ev": "fire", "at": "2026-11-23T10:01:00", "session": SESS, "tokens": 80, "ms": 40,
     "turns": 520, "src": "replay", "terms": ["alpha", "gamma"],
     "ptrs": [{"o": "other-session", "lo": 300, "hi": 305, "tier": "loose",
               "terms": ["gamma"]}]},
    {"ev": "miss", "at": "2026-11-23T10:02:00", "session": SESS, "terms": ["delta"]},
]


class T(unittest.TestCase):
    def setUp(self):
        self.d = vv.summarise(ROWS, rework_tokens=3000, baseline_repeat_pct=5.3)
        self.v = vv.verdicts(self.d)

    def test_counts(self):
        d = self.d
        self.assertEqual(d["turns"], 3)
        self.assertEqual(d["fires"], 2)
        self.assertEqual(d["misses"], 1)
        self.assertEqual(d["ptrs"], 3)
        self.assertEqual(d["strict"], 1)
        self.assertEqual(d["loose"], 1)
        self.assertEqual(d["announce"], 1)
        self.assertEqual(d["replay"], 1)

    def test_archive_window_uses_the_announce_span(self):
        self.assertEqual((self.d["arch_lo"], self.d["arch_hi"]), (200, 900))
        self.assertEqual(self.d["arch_session"], SESS)

    def test_repeat_topics_are_topics_seen_in_two_or_more_fires(self):
        self.assertEqual(self.d["repeat_topics"], 1)      # alpha only; terms dedup per fire

    def test_cost_model_arithmetic(self):
        d = self.d
        self.assertAlmostEqual(d["tok_turn"], 180 / 3, places=1)
        self.assertAlmostEqual(d["breakeven_pct"], 100 * (180 / 3) / 3000, places=2)
        self.assertAlmostEqual(
            d["avoid_pct"], 100 * d["breakeven_pct"] / 5.3, places=1)
        self.assertAlmostEqual(
            d["net_tok_turn"], 0.053 * 3000 - d["tok_turn"], places=1)

    def test_sensitivity_moves_inversely(self):
        self.assertGreater(self.d["tok_turn_1500"], self.d["tok_turn_6000"])

    def test_unmeasurable_read_back_is_never_zero(self):
        self.assertEqual(self.d["read_back"], "n/a - not measured")

    def test_every_placeholder_in_every_template_is_filled(self):
        for name in ("reply-high-level.md", "reply-detailed.md", "value-report.html"):
            with open(os.path.join(ROOT, "templates", name), encoding="utf-8") as fh:
                raw = fh.read()
            out = vv.fill(raw, self.d, self.v)
            left = [w for w in out.split("{{")[1:]]
            self.assertEqual(left, [], "%s left %d placeholders" % (name, len(left)))
            self.assertNotIn("None", out.split("<style>")[0] if "<style>" in out else out)


if __name__ == "__main__":
    unittest.main()
