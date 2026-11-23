"""Regressions for the gate, cross-session pool, hook and metrics defects found on a live
846k-message store.

Each test names the measured failure it prevents, because a threshold that is only
"reasonable" cannot be reviewed -- but a number that was wrong can.
"""

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(os.path.dirname(HERE), "verbatim_recall.py")
spec = importlib.util.spec_from_file_location("verbatim_recall", MOD)
vr = importlib.util.module_from_spec(spec)
sys.modules["verbatim_recall"] = vr
spec.loader.exec_module(vr)

LOG = tempfile.mkdtemp(prefix="vr-reg-")
vr.configure({"metrics.path": LOG, "metrics.tag": "test"})

SID = "reg-session"


def rows_for(spec_rows, filler=60):
    """A synthetic compacted region: `filler` neutral turns plus the named ones."""
    out = []
    for i in range(1, filler + 1):
        out.append({"id": i, "role": "assistant",
                    "content": "Routine note %d about the harness and its widgets." % i})
    for mid, role, body in spec_rows:
        out.append({"id": mid, "role": role, "content": body})
    return out


class Handle(unittest.TestCase):
    """The pointer's id must be the turn that matched, never its neighbour."""

    def setUp(self):
        vr._cache.clear()
        vr._announced.clear()

    def test_handle_is_the_matched_turn_not_the_summary_before_it(self):
        # On the live store the ink named id 836988 while the matched turn was 836990,
        # and 836988 was a [CONTEXT COMPACTION] block -- the reader opened the exact
        # paraphrase this tool exists to replace.
        rows = rows_for([
            (200, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns "
                          "were compacted. The user asked about the zerblat rig."),
            (201, "assistant", "Compaction summary again, mentions zerblat."),
            (202, "user", "how did the zerblat rig calibration finish up then"),
        ])
        # if a summary were indexed it would out-score the real turn on length
        idx = vr.build_index(rows)
        hits = vr.match(idx, "zerblat rig calibration")
        self.assertTrue(hits, "nothing matched at all")
        block = vr.recall(session_id=SID, user_message="zerblat rig calibration",
                          source=lambda _s: rows, cross=False)
        self.assertIsNotNone(block)
        ctx = block["context"]
        named = [int(x) for x in re.findall(r"around_message_id=(\d+)", ctx)]
        self.assertTrue(named, "pointer carried no readable handle: %r" % ctx[:200])
        for mid in named:
            self.assertNotEqual(mid, 200, "the handle points at a compaction summary")
            self.assertNotEqual(mid, 201, "the handle points at a compaction summary")


class Determinism(unittest.TestCase):
    """One query, one answer -- a pointer that moves cannot be tested or audited."""

    def setUp(self):
        vr._cache.clear()
        vr._announced.clear()

    def test_equal_scores_resolve_to_a_fixed_turn(self):
        # Live, one query over one session returned 836990, 839161 and 846927 on
        # different runs, all scoring 7.40: the tie was broken by set order.
        rows = rows_for([(100, "user", "the alphaware build finished"),
                         (300, "user", "the betaware build finished")])
        idx = vr.build_index(rows)
        picks = [vr.match(idx, "alphaware betaware")[0][0] for _ in range(5)]
        self.assertEqual(len(set(picks)), 1, "same query, different answers: %r" % picks)
        self.assertEqual(picks[0], 300, "tie should resolve to the newer turn")

    def test_recall_twice_returns_the_same_span(self):
        rows = rows_for([(100, "user", "the alphaware build finished"),
                         (300, "user", "the betaware build finished")])
        src = lambda _s: rows

        def spans(txt):
            # the announce line is emitted once per region growth by design, so it is
            # not part of "did the answer move" -- only the pointers are
            keep = [l for l in txt.splitlines() if "summarized away but" not in l]
            return re.findall(r"around_message_id=(\d+)", "\n".join(keep))

        a = vr.recall(session_id=SID, user_message="alphaware betaware", source=src,
                      cross=False)["context"]
        vr._cache.clear()
        vr._announced.clear()
        b = vr.recall(session_id=SID, user_message="alphaware betaware", source=src,
                      cross=False)["context"]
        self.assertEqual(spans(a), spans(b))
        self.assertTrue(spans(a), "no pointer to compare")


class AbsentWords(unittest.TestCase):
    """A word the region never uses must lower confidence, not vanish from the maths."""

    def setUp(self):
        vr._cache.clear()
        vr._announced.clear()

    def test_absent_content_words_block_the_strict_tier(self):
        # Live: "zzqqxx nonsense control" fired STRICT at cov=1.00 on "control"
        # alone, because 2 of 3 words were absent and dropped from the denominator.
        rows = rows_for([(i, "tool", "control") for i in (400, 500)])
        idx = vr.build_index(rows)
        hits = vr.match(idx, "zzqqxx nonsense control")
        strict = [h for h in hits if h[3] == "strict"]
        self.assertEqual(strict, [], "absent words did not count: %r" % (hits,))

    def test_a_query_whose_words_are_all_absent_is_silent(self):
        idx = vr.build_index(rows_for([(400, "tool", "control")]))
        self.assertEqual(vr.match(idx, "zzqqxx wwllqq vbnmpq"), [])

    def test_a_fully_present_query_still_fires_strict(self):
        rows = rows_for([(600, "user", "the zerblat rig calibration finished cleanly")])
        idx = vr.build_index(rows)
        hits = vr.match(idx, "zerblat rig calibration")
        self.assertEqual([h[3] for h in hits], ["strict"])

    def test_a_long_ordinary_word_is_not_a_handle(self):
        # Live: "capitulation" (12 letters) appears in 47 turns of a 1,889-turn region
        # and fired STRICT alone. Length is not a handle, and df=12 is not <= 2.
        rows = [{"id": i, "role": "assistant",
                 "content": "Routine note %d about the harness." % i}
                for i in range(1, 301)]
        for mid in range(400, 412):
            rows.append({"id": mid, "role": "assistant",
                         "content": "the configuration was adjusted again %d" % mid})
        idx = vr.build_index(rows)
        hits = vr.match(idx, "zzqqxx nonsense configuration")
        self.assertEqual([h for h in hits if h[3] == "strict"], [],
                         "a long ordinary word still fires STRICT: %r" % (hits,))

    def test_a_handle_shaped_rare_term_still_fires_strict(self):
        # the positive control: shape (digit) AND df <= 2 still stands alone
        rows = rows_for([(410, "user", "the sha256 digest matched the archive")])
        idx = vr.build_index(rows)
        hits = vr.match(idx, "zzqqxx nonsense sha256")
        self.assertEqual([h[3] for h in hits], ["strict"])


class CrossCost(unittest.TestCase):
    """The candidate query must not score every match in the store on every turn."""

    def setUp(self):
        # save/restore the whole config: configure({}) resets to defaults, which would
        # also drop this module's metrics.path/metrics.tag and starve later tests
        self._cfg = dict(vr.CONFIG)
        self.addCleanup(vr.configure, self._cfg)
        self.dir = tempfile.mkdtemp(prefix="vr-reg-db-")
        self.db = os.path.join(self.dir, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(
            "create table messages (id integer primary key, session_id text, role text,"
            " content text, timestamp real);"
            "create table sessions (id text primary key, source text, user_id text,"
            " title text, display_name text, started_at real, last_activity_at real);"
            "create virtual table messages_fts using fts5(content);")
        con.execute("insert into sessions values ('old','telegram','me','probook','p',1.0,1.0)")
        con.execute("insert into sessions values ('aux','telegram','me','asking','a',1.0,1.0)")
        con.execute("insert into messages (id, session_id, role, content) values "
                    "(1,'old','assistant','probook rebuilt as ubuntu server, firmware fix')")
        for i in range(2, 40):
            con.execute("insert into messages (id, session_id, role, content) values "
                        "(?, 'old', 'tool', 'ubuntu ubuntu ubuntu routine output')", (i,))
        con.executemany("insert into messages_fts (rowid, content) values (?,?)",
                        [(r[0], r[1]) for r in
                         con.execute("select id, content from messages").fetchall()])
        con.commit()
        con.close()

    def test_a_common_term_does_not_anchor_the_match(self):
        # 'ubuntu' is carried by 38 rows: it must not be the thing that fetches.
        # Words the store does not use everywhere still fetch, in an OR -- an AND of the
        # rarest terms is what silently lost pointers (see the fall-through test).
        vr.configure({"cross_session.df_cap": 5})
        con = vr._connect_ro(self.db)
        try:
            rows, q, rung, scored = vr._cross_pool(
                con, "probook ubuntu firmware rebuilt", "aux", user_id="me")
        finally:
            con.close()
        self.assertTrue(rows, "nothing found at all")
        self.assertTrue(scored, "the rung returned nothing the scorer could use")
        self.assertNotIn("ubuntu", q.lower(), "a common term is still anchoring the MATCH")
        self.assertNotIn(" AND ", q, "narrowed with AND: %r" % q)
        self.assertEqual(rung, "capped-or")

    def test_a_row_tied_together_by_common_words_is_still_found(self):
        """The regression this ladder exists for: rung 1 returns rows, none can score.

        The only row that shares three query words shares three COMMON ones, so it can
        never appear in a MATCH restricted to the rare words. Accepting rung 1 on
        "returned some rows" left the turn with no pointer at all; the ladder has to
        check that the rows can score before it settles.
        """
        con = sqlite3.connect(self.db)
        # three common terms: 6 rows each, above the cap below
        for i, word in enumerate(("kettle", "ledger", "throttle")):
            for k in range(6):
                con.execute("insert into messages (id, session_id, role, content) values "
                            "(?, 'old', 'tool', ?)", (300 + i * 10 + k, "%s output" % word))
        # two rare terms, each in one row, each alone: reachable, unscorable
        con.execute("insert into messages (id, session_id, role, content) values "
                    "(400,'old','tool','zircon reading')")
        con.execute("insert into messages (id, session_id, role, content) values "
                    "(401,'old','tool','quartzley reading')")
        # the row that answers the question: three words, all of them common
        con.execute("insert into messages (id, session_id, role, content) values "
                    "(500,'old','user','kettle ledger throttle rebuild summary')")
        con.executemany("insert into messages_fts (rowid, content) values (?,?)",
                        [(r[0], r[1]) for r in
                         con.execute("select id, content from messages where id >= 300").fetchall()])
        con.commit()
        con.close()
        vr.configure({"cross_session.df_cap": 5})
        con = vr._connect_ro(self.db)
        try:
            rows, q, rung, scored = vr._cross_pool(
                con, "kettle ledger throttle zircon quartzley", "aux", user_id="me")
        finally:
            con.close()
        ids = [r["id"] for r in scored]
        self.assertIn(500, ids, "lost the row that only common words tie together (%r)" % (rung,))
        self.assertEqual(rung, "all-or")

    def test_a_two_word_answer_is_still_reachable(self):
        con = vr._connect_ro(self.db)
        try:
            # one term is absent from the store: the ladder must loosen, not give up
            rows, q, rung, scored = vr._cross_pool(con, "probook lemonspade firmware", "aux", user_id="me")
        finally:
            con.close()
        self.assertTrue(rows, "gave up instead of loosening the MATCH")
        self.assertTrue("proboo" in q or "firmwa" in q, "unexpected match string: %r" % q)

    def _reset_store(self, rows):
        con = sqlite3.connect(self.db)
        con.execute("delete from messages")
        con.execute("delete from messages_fts")
        con.executemany("insert into messages (id, session_id, role, content) values (?,?,?,?)",
                        rows)
        con.executemany("insert into messages_fts (rowid, content) values (?,?)",
                        [(r[0], r[3]) for r in rows])
        con.commit()
        con.close()

    def _cfg_with(self, **kv):
        cfg = dict(vr.CONFIG)
        cfg.update(kv)
        return cfg

    def test_one_informative_term_still_fetches_its_row(self):
        # Other three words are above df_cap and 'rebuild' is absent: exactly one
        # informative term. Requiring >=2 skipped the precise rung and let the
        # newest-first OR return generic rows instead of the answer.
        common = "ubuntu server output routine"
        rows = [(10, "old", "user", "ubuntu server output firmware probook")]
        rows += [(i, "old", "assistant", common) for i in range(100, 400)]
        self._reset_store(rows)
        vr.configure(self._cfg_with(**{"cross_session.df_cap": 20}))
        con = vr._connect_ro(self.db)
        try:
            got, q, rung, scored = vr._cross_pool(
                con, "ubuntu server output probook rebuild", "aux", user_id="me")
        finally:
            con.close()
        self.assertIn(10, [r["id"] for r in scored],
                      "lost the only rare anchor (%r)" % (rung,))
        self.assertEqual(rung, "capped-or")

    def test_a_rare_anchor_past_the_probe_window_is_still_reachable(self):
        # Six ubiquitous words then the rare anchor at position 7. A MATCH built only
        # from the probed prefix dropped it, and the generic fallback rows could still
        # score on the six common words -- a wrong pointer, not a miss.
        common = "alpha bravo delta gamma kappa sigma"
        rows = [(10, "old", "user", common + " zircon")]
        rows += [(i, "old", "assistant", common) for i in range(100, 400)]
        self._reset_store(rows)
        vr.configure(self._cfg_with(**{"cross_session.df_cap": 20}))
        con = vr._connect_ro(self.db)
        try:
            got, q, rung, scored = vr._cross_pool(con, common + " zircon", "aux", user_id="me")
        finally:
            con.close()
        self.assertIn(10, [r["id"] for r in scored],
                      "a rare anchor at position 7 was dropped (%r)" % (rung,))
        self.assertIn("zircon", q.lower(), "the fallback MATCH lost the anchor: %r" % q)
        self.assertEqual(rung, "capped-or")


class HookCross(unittest.TestCase):
    """The plugin hook passes no source; the default source must still feed the cross pass."""

    def setUp(self):
        self._cfg = dict(vr.CONFIG)
        self.addCleanup(vr.configure, self._cfg)
        self.dir = tempfile.mkdtemp(prefix="vr-hook-db-")
        self.db = os.path.join(self.dir, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(
            "create table messages (id integer primary key, session_id text, role text,"
            " content text, timestamp real, compacted integer default 0);"
            "create table sessions (id text primary key, source text, user_id text,"
            " title text, display_name text, started_at real, last_activity_at real);"
            "create virtual table messages_fts using fts5(content);")
        con.execute("insert into sessions values ('aux','telegram','me','asking','a',1.0,1.0)")
        con.execute("insert into sessions values ('old','telegram','me','probook','p',1.0,1.0)")
        con.execute("insert into messages (id,session_id,role,content) values "
                    "(1,'old','user','probook rebuilt as ubuntu server, firmware fix')")
        con.executemany("insert into messages_fts (rowid,content) values (?,?)",
                        con.execute("select id,content from messages").fetchall())
        con.commit()
        con.close()
        cfg = dict(vr.CONFIG)
        cfg["db_path"] = self.db
        vr.configure(cfg)

    def test_recall_without_a_source_still_crosses(self):
        vr._cache.clear()
        vr._announced.clear()
        vr._cross_turn.clear()
        vr._cross_seen.clear()
        block = vr.recall(session_id="aux",
                          user_message="probook rebuilt ubuntu firmware", cross=True)
        self.assertIsNotNone(block, "no pointer at all")
        self.assertIn("ANOTHER SESSION", block["context"])


class ReportAndGlobals(unittest.TestCase):
    """The four leftovers: index shape check, racy globals, diag on the log, terms leak."""

    def setUp(self):
        self._cfg = dict(vr.CONFIG)
        self.addCleanup(vr.configure, self._cfg)
        self.dir = tempfile.mkdtemp(prefix="vr-leftovers-")
        self.db = os.path.join(self.dir, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(
            "create table messages (id integer primary key, session_id text, role text,"
            " content text, timestamp real, compacted integer default 0);"
            "create table sessions (id text primary key, source text, user_id text,"
            " title text, display_name text, started_at real, last_activity_at real);"
            "create virtual table messages_fts using fts5(content);")
        con.execute("insert into sessions values ('aux','telegram','me','a','a',1.0,1.0)")
        con.execute("insert into messages (id,session_id,role,content,compacted) values "
                    "(7,'aux','user','probook rebuilt as ubuntu server',1)")
        con.execute("insert into messages (id,session_id,role,content,compacted) values "
                    "(8,'aux','assistant','the firmware fix was the network stack',1)")
        con.commit()
        con.close()

    def _configure(self, **kv):
        cfg = dict(vr.CONFIG)
        cfg["db_path"] = self.db
        cfg["metrics.enabled"] = False
        cfg.update(kv)
        vr.configure(cfg)

    def test_the_shape_check_counts_without_reading_bodies(self):
        # reading every row's content cost 14.6-19.1 ms per turn to decide "unchanged".
        class BodyReader:
            """A source that explodes if anyone iterates it -- proves the store path wins."""
            path = self.db

            def __call__(self, sid):
                raise AssertionError("iterated the source instead of the store")

        self.assertEqual(vr._signature(BodyReader(), "aux"), (2, 8))

    def test_shape_check_still_works_for_a_plain_callable(self):
        self.assertEqual(vr._signature(
            lambda sid: [{"id": 1}, {"id": 5}, {"id": 3}], "any"), (3, 5))

    def test_the_cross_pass_reports_its_rung_without_a_module_global(self):
        d: dict = {}
        vr.cross_hits("aux", "probook rebuilt ubuntu", db_path=self.db, diag=d)
        self.assertIn("rung", d)
        self.assertIn("match", d)
        self.assertFalse(hasattr(vr, "_cross_last_rung"),
                         "the racy module global is back")

    def test_store_terms_off_keeps_the_words_out_of_the_log(self):
        self._configure()
        vr.configure({"db_path": self.db, "metrics.enabled": True,
                      "metrics.path": os.path.join(self.dir, "logs"),
                      "metrics.store_terms": False})
        con = sqlite3.connect(self.db)
        con.execute("insert into messages (id,session_id,role,content) values "
                    "(31,'aux','assistant','zzrare probook rebuilt ubuntu server firmware')")
        con.commit()
        con.close()
        con = sqlite3.connect(self.db)
        con.execute("insert into messages_fts (rowid, content) select id, content "
                    "from messages where id = 31")
        con.commit()
        con.close()
        vr._cache.clear()
        vr.recall(session_id="aux", user_message="probook rebuilt ubuntu firmware",
                  cross=False)
        lines = vr.load_metrics(30)
        fires = [e for e in lines if e.get("ev") == "fire"]
        self.assertTrue(fires, "nothing was logged, so this proves nothing")
        for e in fires:
            for p in e.get("ptrs") or []:
                self.assertEqual(p.get("terms"), [],
                                 "metrics.store_terms=false still logged the words")


class MetricsIsolated(unittest.TestCase):
    """A test run must never be readable as live traffic."""

    def test_events_carry_the_tag_and_land_in_the_test_dir(self):
        rows = rows_for([(700, "user", "the quibblex rig calibration finished")])
        vr._cache.clear()
        vr.recall(session_id=SID, user_message="quibblex rig calibration",
                  source=lambda _s: rows, cross=False)
        files = [f for f in os.listdir(LOG) if f.startswith("events-")]
        self.assertTrue(files, "no event file in the isolated dir")
        import json
        with open(os.path.join(LOG, files[0]), encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh.read().splitlines() if x]
        self.assertTrue(lines)
        self.assertTrue(all(l.get("src") == "test" for l in lines),
                        "untagged rows can be counted as live: %r" % lines[:1])


if __name__ == "__main__":
    unittest.main()
