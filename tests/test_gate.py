"""Gate tests: strict fires, loose is labelled, misses are silent.

Run:  python -m unittest discover -s tests -v     (stdlib only, no network, no DB)
"""

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(os.path.dirname(HERE), "verbatim_recall.py")
spec = importlib.util.spec_from_file_location("verbatim_recall", MOD)
vr = importlib.util.module_from_spec(spec)
sys.modules["verbatim_recall"] = vr
spec.loader.exec_module(vr)

SID = "test-session"
STRICT_ID = 120
LOOSE_ID = 240


def build_rows():
    """A synthetic compacted region: filler, boilerplate, one gold turn, one partial turn."""
    rows = []
    mid = 1
    for i in range(300):
        body = (f"Ran the build step and it exited cleanly. "
                f"Note about item{i} and widget{i} in the harness.")
        rows.append({"id": mid, "role": "assistant", "content": body})
        mid += 1
    # a turn with near-identifier rarity: the gold hit
    rows.append({"id": STRICT_ID, "role": "user",
                 "content": "did you help before with the probook rebuild to ubuntu?"})
    rows.append({"id": STRICT_ID + 1, "role": "assistant",
                 "content": "Yes: probook rebuild to ubuntu, grub then the wifi firmware."})
    # terms that are common but not boilerplate: 20 turns carry both
    for i in range(20):
        rows.append({"id": 200 + i, "role": "assistant",
                     "content": f"The context and the model both matter for itemx{i}."})
    # the loose target: carries 2 of the query's words, nothing rare
    rows.append({"id": LOOSE_ID, "role": "assistant",
                 "content": "On this host the context and the model are pinned to 64k."})
    return rows


ROWS = build_rows()


def source(_session_id):
    return ROWS


class Gate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        idx = vr.build_index(ROWS)
        assert idx, "index build failed"
        cls.idx = idx

    # ---- tier 1: strict
    def test_strict_fires_on_gold_turn(self):
        hits = vr.match(self.idx, "did the probook rebuild to ubuntu work?")
        self.assertTrue(hits, "gold query produced no pointer")
        mid, _sc, terms, tier, cov = hits[0]
        self.assertEqual(tier, "strict")
        self.assertEqual(mid, STRICT_ID)
        self.assertGreaterEqual(cov, vr.COVERAGE)
        self.assertIn("probook", terms)

    def test_strict_is_not_labelled_low_confidence(self):
        out = vr.recall(session_id=SID, user_message="probook rebuild to ubuntu", source=source)
        self.assertIsNotNone(out)
        self.assertIn("matched:", out["context"])
        self.assertNotIn("LOW CONFIDENCE", out["context"])

    # ---- tier 2: loose, explicitly labelled
    def test_loose_fires_and_is_labelled(self):
        hits = vr.match(self.idx, "op3 local model context settings")
        self.assertTrue(hits, "partial-overlap query produced no pointer (false negative)")
        _mid, _sc, _terms, tier, _cov = hits[0]
        self.assertEqual(tier, "loose")
        out = vr.recall(session_id=SID, user_message="op3 local model context settings",
                        source=source)
        self.assertIn("LOW CONFIDENCE", out["context"])
        self.assertIn("query", out["context"])
        self.assertIn("unrelated turn", out["context"])

    # ---- silence
    def test_absent_words_are_silent(self):
        self.assertIsNone(vr.recall(session_id=SID, announce=False,
                                    user_message="xylophone quasar zeppelin", source=source))

    def test_one_common_word_is_silent(self):
        self.assertIsNone(vr.recall(session_id=SID, announce=False,
                                    user_message="context", source=source))

    def test_boilerplate_is_not_evidence(self):
        # "exited cleanly" is in every filler turn and must not anchor a pointer
        self.assertIsNone(vr.recall(session_id=SID, announce=False,
                                    user_message="exited cleanly harness", source=source))

    def test_empty_region_and_empty_query(self):
        self.assertIsNone(vr.build_index([]))
        self.assertIsNone(vr.recall(session_id=SID, user_message="probook rebuild", source=lambda s: []))

    # ---- announce + shape
    def test_announce_fires_once_per_growth(self):
        vr._announced.clear()
        first = vr.recall(session_id="ann-1", user_message="", source=source)
        self.assertIsNone(first)  # no user message -> nothing at all
        vr._announced.clear()
        msg = "probook rebuild to ubuntu"
        a = vr.recall(session_id="ann-2", user_message=msg, source=source)
        self.assertIn("summarized away but kept verbatim", a["context"])
        vr._cache.clear()
        b = vr.recall(session_id="ann-2", user_message=msg, source=source)
        self.assertNotIn("summarized away but kept verbatim", b["context"])

    def test_pointer_names_reader_call_and_span(self):
        out = vr.recall(session_id=SID, user_message="probook rebuild to ubuntu",
                        source=source, announce=False)
        ctx = out["context"]
        self.assertIn(f"session_id='{SID}'", ctx)
        self.assertIn("around_message_id=", ctx)
        self.assertRegex(ctx, r"ids \d+-\d+")

    def test_reader_hint_is_overridable(self):
        vr._announced.clear()
        out = vr.recall(session_id=SID, user_message="probook rebuild to ubuntu", source=source,
                        reader_hint="open_span(session={session_id}, id={message_id})")
        self.assertIn("open_span(session=", out["context"])

    def test_fails_open_on_a_broken_source(self):
        def boom(_sid):
            raise RuntimeError("db locked")
        self.assertIsNone(vr.recall(session_id=SID, user_message="probook rebuild", source=boom))


if __name__ == "__main__":
    unittest.main()
