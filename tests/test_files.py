"""Tests for the file corpus: a fresh turn gets the dated record for its own entity.

The promise under test is narrow and checkable: given a work order that names a site
(and optionally a row), the corpus returns the file that was last written about that
entity -- not a transcript turn, because no turn *is* the edit -- and returns nothing
when the message names no entity it knows.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(os.path.dirname(HERE), "vr_files.py")
spec = importlib.util.spec_from_file_location("vr_files", MOD)
vr_files = importlib.util.module_from_spec(spec)
sys.modules["vr_files"] = vr_files
spec.loader.exec_module(vr_files)


class FileCorpusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vrfiles-")
        root = os.path.join(self.tmp, "wl-knowledge")
        trail = os.path.join(root, "sites", "trail-ridge-road")
        other = os.path.join(root, "sites", "castel-dell-ovo")
        os.makedirs(os.path.join(trail, "findings"))
        os.makedirs(other)

        self.facts = os.path.join(trail, "facts.yaml")
        with open(self.facts, "w") as fh:
            fh.write(
                "site: trail-ridge-road.com\n"
                "venues:\n"
                "  - row: 1589\n"
                "    name: Daven Haven Lodge\n"
                "    price: 240\n"
            )
        # an older dated finding about the same venue
        self.finding = os.path.join(trail, "findings", "2026-11-01-trail-ridge-road.md")
        with open(self.finding, "w") as fh:
            fh.write("# Daven Haven Lodge\n\nchecked the price on row 1589\n")
        old = time.time() - 86400 * 20
        os.utime(self.finding, (old, old))

        with open(os.path.join(other, "facts.yaml"), "w") as fh:
            fh.write("site: castel-dell-ovo.com\nname: Castel dell'Ovo\n")
        with open(os.path.join(other, "notes.md"), "w") as fh:
            fh.write("opening hours changed\n")

        self.root = root
        self.index_path = os.path.join(self.tmp, "files-index.json")
        self.idx = vr_files.build(roots=[root], path=self.index_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- the index ---------------------------------------------------------
    def test_build_indexes_sites_and_writes_a_cache(self):
        self.assertIn("trail-ridge-road", self.idx["sites"])
        self.assertTrue(os.path.exists(self.index_path))

    def test_index_has_no_file_bodies(self):
        # the cache is a pointer map, not a copy of the corpus: tokens only.
        with open(self.index_path) as fh:
            raw = fh.read()
        self.assertNotIn("Daven Haven Lodge", raw)
        self.assertLess(len(raw), 20000)

    # ---- the lookup --------------------------------------------------------
    def test_names_the_newest_file_for_the_named_row(self):
        lines, refs = vr_files.lookup(
            "fix the price of Daven Haven Lodge, trail-ridge-road.com row 1589", idx=self.idx
        )
        self.assertTrue(1 <= len(lines) <= 2, lines)      # record, plus a dated one if any
        self.assertTrue(1 <= len(refs) <= 2)
        self.assertIn("trail-ridge-road", lines[0])
        self.assertIn(refs[0]["path"], lines[0])
        self.assertTrue(refs[0]["path"].startswith("sites/trail-ridge-road/"))

    def test_picks_the_newer_of_two_files_on_the_same_venue(self):
        lines, refs = vr_files.lookup("Daven Haven Lodge trail-ridge-road.com", idx=self.idx)
        self.assertEqual(refs[0]["path"], "sites/trail-ridge-road/facts.yaml")

    def test_site_spelled_out_without_a_domain(self):
        lines, _ = vr_files.lookup("update trail ridge road opening hours", idx=self.idx)
        self.assertEqual(len(lines), 1, lines)

    def test_site_only_pointer_is_labelled_as_site_only(self):
        # "update the opening hours" names no entity inside the site: the pointer must say
        # it is the site's latest file, not pretend it is a record about opening hours.
        lines, _ = vr_files.lookup("update the opening hours on castel-dell-ovo.com", idx=self.idx)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("latest file on castel-dell-ovo", lines[0])
        self.assertIn("no word of this order matched beyond the site name", lines[0])

    def test_instruction_verbs_are_not_entity_words(self):
        # "refresh" is what to do to the record, not what the record is about
        _, _, _ = vr_files._entities("x")
        self.assertIn("refresh", vr_files.STOP)
        self.assertIn("stale", vr_files.STOP)

    def test_no_entity_is_silence(self):
        lines, refs = vr_files.lookup("write the monthly performance review", idx=self.idx)
        self.assertEqual((lines, refs), ([], []))

    def test_unknown_site_is_silence(self):
        lines, refs = vr_files.lookup("fix kamakura-daibutsu.com row 12", idx=self.idx)
        self.assertEqual((lines, refs), ([], []))

    def test_pointer_names_the_file_and_asks_for_a_read(self):
        lines, _ = vr_files.lookup("Daven Haven Lodge trail-ridge-road.com row 1589", idx=self.idx)
        self.assertIn("read it before changing anything", lines[0])
        self.assertIn("newest", lines[0])

    def test_volume_does_not_beat_recency(self):
        # the real failure this guards: a long older document that repeats the venue ten
        # times outranked the dated file written about it last week, because ranking was
        # on term count. Recency has to win once the match is distinctive.
        big = os.path.join(self.root, "sites", "trail-ridge-road", "findings",
                           "2026-10-01-season-research.md")
        with open(big, "w") as fh:
            fh.write(("Daven Haven Lodge opening hours and price notes.\n" * 10))
        old = time.time() - 86400 * 40
        os.utime(big, (old, old))
        idx = vr_files.build(roots=[self.root], path=self.index_path)
        _lines, refs = vr_files.lookup(
            "fix the price of Daven Haven Lodge, trail-ridge-road.com row 1589", idx=idx
        )
        self.assertNotEqual(refs[0]["path"], "sites/trail-ridge-road/findings/"
                           "2026-10-01-season-research.md")

    def test_system_envelope_gets_no_pointer(self):
        # tool chatter carries paths and process ids, not work: matching the corpus against
        # it produced confident pointers to unrelated files.
        for msg in (
            "[IMPORTANT: Background process proc_6942395 completed normally (exit code 0)]\n"
            "Command: cd /home/wlmedia/.hermes/cache/scratch && python3 vr_files.py build",
            "[ASYNC DELEGATION BATCH COMPLETE — deleg_9ac9b22f] 1 subagent finished",
            "[OUT-OF-BAND USER MESSAGE] the davens haven price is wrong on trail-ridge-road",
        ):
            self.assertEqual(vr_files.lookup(msg, idx=self.idx), ([], []), msg[:40])

    def test_harness_vocabulary_is_not_entity_evidence(self):
        # "unit", "results", "batch" name the machinery around the work, not the venue
        for w in ("unit", "results", "batch", "background"):
            self.assertIn(w, vr_files.STOP)
        lines, _ = vr_files.lookup("batch unit results trail-ridge-road.com", idx=self.idx)
        self.assertFalse([l for l in lines if "file record for" in l], lines)

    # ---- robustness --------------------------------------------------------
    def test_missing_root_never_raises(self):
        idx = vr_files.build(roots=[os.path.join(self.tmp, "nope")], path=self.index_path)
        self.assertEqual(vr_files.lookup("trail-ridge-road.com", idx=idx), ([], []))

    def test_empty_message_returns_nothing(self):
        self.assertEqual(vr_files.lookup("", idx=self.idx), ([], []))

    def test_same_site_key_in_two_trees_answers_from_its_own_tree(self):
        # same site key, two corpora: each index may only answer with its own paths
        other_root = os.path.join(self.tmp, "second", "wl-knowledge")
        venue = os.path.join(other_root, "sites", "trail-ridge-road", "findings")
        os.makedirs(venue)
        with open(os.path.join(venue, "2026-11-20-second-tree.md"), "w") as fh:
            fh.write("# Daven Haven Lodge\n\nsecond corpus\n")
        idx2 = vr_files.build(roots=[other_root], path=os.path.join(self.tmp, "second-index.json"))
        msg = "Daven Haven Lodge trail-ridge-road.com"
        first = vr_files.lookup(msg, idx=self.idx)[1][0]["path"]
        second = vr_files.lookup(msg, idx=idx2)[1][0]["path"]
        self.assertTrue(first.startswith("sites/trail-ridge-road/"))
        self.assertEqual(second, "sites/trail-ridge-road/findings/2026-11-20-second-tree.md")

    def test_lookup_writes_nothing_to_disk(self):
        # the body search is mechanical and uncached on purpose: a corpus-sized token
        # cache cost hundreds of megabytes, and the pointer is read once per turn.
        home = os.path.join(self.tmp, "hermes-home")
        os.makedirs(home)
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = home
        try:
            vr_files.lookup("Daven Haven Lodge trail-ridge-road.com", idx=self.idx)
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old
        self.assertEqual(os.listdir(home), [])

    def test_load_reuses_a_fresh_cache_instead_of_rewalking(self):
        vr_files.load(refresh=3600, roots=[self.root], path=self.index_path)
        # a file created after the walk must not appear while the cache is fresh
        fresh = os.path.join(self.root, "sites", "trail-ridge-road", "2026-11-23-added.md")
        with open(fresh, "w") as fh:
            fh.write("Daven Haven Lodge row 1589 again\n")
        idx2 = vr_files.load(refresh=3600, roots=[self.root], path=self.index_path)
        rel = [f[0] for f in idx2["sites"]["trail-ridge-road"]]
        self.assertNotIn("sites/trail-ridge-road/2026-11-23-added.md", rel)


class TestFreshSessionHook(unittest.TestCase):
    """The plugin-level promise: a session with no transcript still opens with a pointer.

    A subagent's first turn has an empty store — nothing compacted, nothing to search —
    so the hook must reach the file corpus instead of short-circuiting on "no index".
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "wl-knowledge")
        d = os.path.join(self.root, "sites", "trail-ridge-road", "findings")
        os.makedirs(d)
        with open(os.path.join(self.root, "sites", "trail-ridge-road", "facts.yaml"), "w") as fh:
            fh.write("site: trail-ridge-road.com\nrow: 1589\nname: Daven Haven Lodge\n")
        with open(os.path.join(d, "2026-11-02-daven-haven-price.md"), "w") as fh:
            fh.write("Daven Haven Lodge row 1589 price checked\n")
        self.index_path = os.path.join(self.tmp, "idx.json")
        recall_dir = os.path.dirname(HERE)
        spec = importlib.util.spec_from_file_location(
            "vr_plugin_for_tests", os.path.join(recall_dir, "verbatim_recall.py"))
        self.plugin = importlib.util.module_from_spec(spec)
        sys.modules["vr_plugin_for_tests"] = self.plugin
        spec.loader.exec_module(self.plugin)
        self.plugin.FILES = True
        self.plugin.METRICS = False
        self.plugin._FILES_SEEN.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recall(self, msg, session):
        self.plugin.FILES_ROOTS = (self.root,)
        self.plugin.FILES_INDEX = self.index_path
        self.plugin.FILES_REFRESH = 0
        return self.plugin.recall(session_id=session, user_message=msg, source=None)

    def test_fresh_session_gets_file_pointer(self):
        out = self._recall("fix the price of Daven Haven Lodge, trail-ridge-road.com row 1589",
                           "20990101_000000_fresh0001")
        self.assertIsNotNone(out, out)
        self.assertIn("file record for trail-ridge-road", out["context"])
        self.assertIn("2026-11-02-daven-haven-price.md", out["context"])
        self.assertLess(len(out["context"]), 600)   # a pointer, not a page

    def test_same_session_is_not_told_twice(self):
        first = self._recall("fix the price of Daven Haven Lodge, trail-ridge-road.com row 1589",
                             "20990101_000000_fresh0002")
        self.assertIsNotNone(first)
        again = self._recall("now update the opening hours of Daven Haven Lodge",
                             "20990101_000000_fresh0002")
        self.assertIsNone(again, again)

    def test_no_entity_is_silence(self):
        self.assertIsNone(self._recall("what time is it in Hong Kong", "20990101_000000_fresh0003"))


if __name__ == "__main__":
    unittest.main()
