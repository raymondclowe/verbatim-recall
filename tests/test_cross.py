"""Cross-session tests: a foreign turn is reachable, labelled, and cooled down.

Run:  python -m unittest discover -s tests -v     (stdlib only, no network)
Builds a throwaway SQLite store shaped like a host's state.db.
"""

import importlib.util
import os
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

AUX = "aux-session"          # the session asking the question
OLD = "old-session"          # a foreign session that knows the answer
CRON = "cron-session"        # out of scope by source
STRANGER = "stranger-session"  # another person's chat in the same store
PEER = "peer-a2a"            # agent-to-agent session from another machine
ANSWER_ID = 900
ME = "666023934"


def build_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table messages (
            id integer primary key, session_id text, role text, content text,
            timestamp real, active integer default 1, compacted integer default 0);
        create table sessions (
            id text primary key, source text, user_id text, session_key text,
            chat_id text, chat_type text, thread_id text, display_name text,
            title text, origin_json text, started_at real, last_activity_at real);
        create virtual table messages_fts using fts5(content);
        """
    )
    for i in range(1, 60):
        con.execute("insert into messages (id, session_id, role, content, timestamp) "
                    "values (?,?,?,?,?)",
                    (i, AUX, "assistant",
                     "Routine note %d about the harness and its widgets." % i, 1.0 + i))
    con.execute("insert into messages (id, session_id, role, content, timestamp) "
                "values (?,?,?,?,?)",
                (ANSWER_ID, OLD, "assistant",
                 "Wiped the probook and rebuilt it as Ubuntu Server 24.04, hostname PR1, "
                 "grub then the wifi firmware needed a fix.", 20000.0))
    con.execute("insert into messages (id, session_id, role, content, timestamp) "
                "values (?,?,?,?,?)",
                (ANSWER_ID + 1, CRON, "assistant",
                 "Probook rebuilt to ubuntu by the nightly cron job.", 20001.0))
    con.execute("insert into messages (id, session_id, role, content, timestamp) "
                "values (?,?,?,?,?)",
                (ANSWER_ID + 2, AUX, "assistant",
                 "I have not touched the probook rebuild or its ubuntu install.", 20002.0))
    for sid, src, uid, title, started in (
            (AUX, "telegram", ME, "current chat", 1.0),
            (OLD, "telegram", ME, "Salvage RAM from the old ProBook", 19000.0),
            (CRON, "cron", "cron-owner", "nightly job", 19001.0),
            (STRANGER, "telegram", "someone-else", "their own probook notes", 19002.0),
            (PEER, "a2a", "ip:192.168.0.83", "peer asked about the probook", 19003.0)):
        con.execute("insert into sessions (id, source, user_id, title, started_at)"
                    " values (?,?,?,?,?)", (sid, src, uid, title, started))
    con.execute("insert into messages (id, session_id, role, content, timestamp) "
                "values (?,?,?,?,?)",
                (ANSWER_ID + 3, STRANGER, "assistant",
                 "Probook rebuild to ubuntu, probook ubuntu rebuilt, firmware notes.", 20003.0))
    con.execute("insert into messages (id, session_id, role, content, timestamp) "
                "values (?,?,?,?,?)",
                (ANSWER_ID + 4, PEER, "assistant",
                 "Asked to check the probook ubuntu rebuild and its firmware.", 20004.0))
    con.executemany("insert into messages_fts (rowid, content) values (?,?)",
                    [(r[0], r[1]) for r in
                     con.execute("select id, content from messages").fetchall()])
    con.commit()
    con.close()


def aux_source(_sid):
    """The asking session has no compacted region at all (a brand-new chat)."""
    return []


class Cross(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="vr-cross-")
        cls.db = os.path.join(cls.dir, "state.db")
        build_db(cls.db)

    def setUp(self):
        vr._cross_seen.clear()
        vr._cross_turn.clear()
        vr._announced.clear()
        vr._cache.clear()

    # ---- the whole point
    def test_new_session_finds_an_old_session(self):
        out = vr.recall(session_id=AUX, user_message="did the probook rebuild to ubuntu work?",
                        source=aux_source, cross_db=self.db)
        self.assertIsNotNone(out, "no pointer at all: cross-session did not fire")
        ctx = out["context"]
        self.assertIn("LOW CONFIDENCE", ctx)
        self.assertIn(f"session_id='{OLD}'", ctx)
        self.assertIn("around_message_id=", ctx)

    def test_pointer_span_sits_inside_the_owning_session(self):
        hits = vr.cross_hits(AUX, "probook rebuilt ubuntu hostname grub", db_path=self.db)
        self.assertTrue(hits)
        hit = [h for h in hits if h["session_id"] == OLD][0]
        self.assertLessEqual(hit["lo"], hit["id"])
        self.assertGreaterEqual(hit["hi"], hit["id"])

    def test_own_session_is_never_quoted_back(self):
        hits = vr.cross_hits(AUX, "probook rebuild ubuntu", db_path=self.db)
        self.assertTrue(all(h["session_id"] != AUX for h in hits))

    def test_another_persons_chat_is_never_quoted(self):
        hits = vr.cross_hits(AUX, "probook rebuild ubuntu firmware", db_path=self.db)
        self.assertTrue(hits)
        self.assertTrue(all(h["session_id"] != STRANGER for h in hits),
                        "another user's session leaked into this prompt: %r" % (hits,))

    def test_a2a_peer_is_in_scope_despite_its_own_identity(self):
        hits = vr.cross_hits(AUX, "probook rebuild ubuntu firmware", db_path=self.db,
                             top_k=3)
        self.assertIn(PEER, [h["session_id"] for h in hits])

    def test_out_of_scope_source_is_ignored(self):
        hits = vr.cross_hits(AUX, "probook rebuild ubuntu by the nightly cron job",
                             db_path=self.db)
        self.assertTrue(all(h["session_id"] != CRON for h in hits),
                        "cron session leaked into scope")

    def test_foreign_pointer_joins_a_local_hit(self):
        rows = [{"id": 1, "role": "assistant",
                 "content": "Note: probook rebuild to ubuntu is done."}]
        out = vr.recall(session_id=AUX, user_message="probook rebuild ubuntu firmware",
                        source=lambda s: rows, cross_db=self.db, announce=False)
        ctx = out["context"]
        self.assertIn("summarized away", ctx)          # local pointer
        self.assertIn(f"session_id='{OLD}'", ctx)      # plus the foreign one

    # ---- gates
    def test_one_common_word_does_not_cross(self):
        self.assertEqual(vr.cross_hits(AUX, "harness widgets", db_path=self.db), [])

    def test_absent_words_find_nothing(self):
        self.assertEqual(vr.cross_hits(AUX, "cardamom bayou sledgehammer",
                                       db_path=self.db), [])

    def test_cooldown_stops_the_same_session_repeating(self):
        msg = "probook rebuild ubuntu firmware"
        first = vr.recall(session_id=AUX, user_message=msg, source=aux_source,
                          cross_db=self.db)
        self.assertIn("ANOTHER SESSION may hold this", first["context"])
        second = vr.recall(session_id=AUX, user_message=msg, source=aux_source,
                           cross_db=self.db)
        self.assertIsNone(second, "foreign session repeated inside the cooldown")

    def test_cross_can_be_switched_off(self):
        self.assertIsNone(vr.recall(session_id=AUX, user_message="probook rebuild ubuntu",
                                    source=aux_source, cross=False, cross_db=self.db))

    def test_store_without_fts_is_silent_not_fatal(self):
        bare = os.path.join(self.dir, "bare.db")
        con = sqlite3.connect(bare)
        con.execute("create table messages (id integer primary key, session_id text)")
        con.commit()
        con.close()
        self.assertFalse(vr.fts_capable(bare))
        self.assertEqual(vr.cross_hits(AUX, "probook rebuild ubuntu", db_path=bare), [])

    def test_missing_store_is_silent(self):
        self.assertEqual(vr.cross_hits(AUX, "probook rebuild ubuntu",
                                       db_path=os.path.join(self.dir, "nope.db")), [])

    def test_fts_query_is_safely_quoted(self):
        q = vr.fts_query('probook " OR * rebuild NEAR( ubuntu')
        self.assertTrue(q)
        self.assertNotIn("NEAR(", q)
        self.assertEqual(q.count('"') % 2, 0)


if __name__ == "__main__":
    unittest.main()
