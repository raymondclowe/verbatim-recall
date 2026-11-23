"""Replay real user turns through the live recall path so the metrics log has real rows.

This is NOT synthetic data: every message comes from state.db, and each row is the
plugin's real verdict on a real turn. Every row is tagged src="replay" so live rows can
be told apart from backfilled ones.
"""
import json
import os
import sqlite3
import sys

REPO = r"C:/Users/raymo/repos/verbatim-recall"
sys.path.insert(0, REPO)
import verbatim_recall as vr

# Tag every row this run writes, so a backfilled log is never mistaken for live traffic.
_orig_metric = vr.metric


def _tagged_metric(ev, **fields):
    fields.setdefault("src", "replay")
    return _orig_metric(ev, **fields)


vr.metric = _tagged_metric

DB = os.path.join(os.environ["LOCALAPPDATA"], "hermes", "state.db")
N_TURNS = int(sys.argv[1]) if len(sys.argv) > 1 else 40
SESS = "20261112_172849_f7171a9f"

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT id, session_id, content FROM messages "
    "WHERE role='user' AND session_id=? AND content IS NOT NULL "
    "ORDER BY id DESC LIMIT ?", (SESS, N_TURNS)).fetchall()
rows = list(reversed(rows))
print("replaying %d real user turns of %s" % (len(rows), SESS))
print("metrics dir:", vr.metrics_dir())

fired = missed = reads = 0
for r in rows:
    msg = r["content"]
    out = vr.recall(session_id=r["session_id"], user_message=msg)
    ctx = (out or {}).get("context") or ""
    if ctx:
        fired += 1
        # simulate the agent acting on the first pointer it was handed
        ids = []
        for line in ctx.splitlines():
            for tok in ("around_message_id=", "message_id="):
                if tok in line:
                    try:
                        ids.append(int(line.split(tok)[1].split(",")[0]
                                       .split(")")[0].strip()))
                    except Exception:
                        pass
        ptrs = (out or {}).get("pointers") or [None]
        if ids and "--reads" in sys.argv:
            p = ptrs[0]
            try:
                vr.on_tool_call(session_id=r["session_id"],
                                tool_name="session_search",
                                arguments={"session_id": (p or {}).get("o") or r["session_id"],
                                           "around_message_id": ids[0], "window": 20})
                reads += 1
            except Exception as exc:
                if reads == 0 and fired == 1:
                    print("on_tool_call failed:", type(exc).__name__, exc)
    else:
        missed += 1

print("replay done: %d fired, %d no-pointer, %d simulated reads" % (fired, missed, reads))

rep = vr.report(days=30)
print("log now holds %d events over %d turns (fired %d)"
      % (rep["events"], rep["turns"], rep["fires"]))
