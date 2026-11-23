"""Regressions for the four gate defects found on a live 846k-message store.

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


class CrossCost(unittest.TestCase):
    """The candidate query must not score every match in the store on every turn."""

    def setUp(self):
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

    def test_the_rarest_terms_are_anded(self):
        con = vr._connect_ro(self.db)
        try:
            # four terms, one of them (ubuntu) carried by 38 rows: the common one must
            # not be scored, because scoring it is what made every turn cost 95-135 ms
            rows, q = vr._cross_pool(con, "probook ubuntu firmware rebuilt", "aux")
        finally:
            con.close()
        self.assertTrue(rows, "the AND ladder found nothing")
        self.assertIn(" AND ", q, "still an OR over every term: %r" % q)
        self.assertNotIn("ubuntu", q.lower(), "a common term is still being scored")
        self.assertEqual(len(q.split(" AND ")), 3)

    def test_a_two_word_answer_is_still_reachable(self):
        con = vr._connect_ro(self.db)
        try:
            # one term is absent from the store: the ladder must loosen, not give up
            rows, q = vr._cross_pool(con, "probook lemonspade firmware", "aux")
        finally:
            con.close()
        self.assertTrue(rows, "gave up instead of falling back to the rarest pair")
        self.assertTrue("proboo" in q or "firmwa" in q, "unexpected match string: %r" % q)


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
