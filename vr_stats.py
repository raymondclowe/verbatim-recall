#!/usr/bin/env python3
"""vr_stats — read the verbatim-recall event log and answer "is it helping?".

Headline first, then a table, then deeper detail only when asked for.

    python vr_stats.py                        # last 30 days
    python vr_stats.py --days 7
    python vr_stats.py --since 2026-11-01 --until 2026-11-23
    python vr_stats.py --session 20261112_172849_f7171a9f
    python vr_stats.py --detail days          # per-day table
    python vr_stats.py --detail sessions      # which other sessions fed it
    python vr_stats.py --detail fires         # every fire: what matched, tokens
    python vr_stats.py --detail misses        # every turn with no pointer + reason
    python vr_stats.py --detail terms         # top matched terms
    python vr_stats.py --detail all
    python vr_stats.py --json                 # machine-readable aggregate

Exit code 0 with data, 3 when the log is empty (so a skill can say "nothing logged yet").
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import verbatim_recall as vr  # noqa: E402


def _pct(part, whole):
    return ("%d%%" % round(100.0 * part / whole)) if whole else "-"


def _table(rows, cols, indent="  "):
    """rows: list of tuples; cols: list of (title, right_align)."""
    if not rows:
        return [indent + "(none)"]
    cells = [[str(c) for c in r] for r in rows]
    widths = [max([len(cols[i][0])] + [len(r[i]) for r in cells])
              for i in range(len(cols))]
    out = [indent + "  ".join(
        (t.rjust(widths[i]) if cols[i][1] else t.ljust(widths[i]))
        for i, (t, _) in enumerate(cols)).rstrip()]
    out.append(indent + "  ".join("-" * w for w in widths))
    for r in cells:
        out.append(indent + "  ".join(
            (r[i].rjust(widths[i]) if cols[i][1] else r[i].ljust(widths[i]))
            for i in range(len(cols))).rstrip())
    return out


def headline(rep):
    """Two or three plain sentences: did it fire, did the pointers get read, was it cheap."""
    if not rep["turns"]:
        return ["No recall turns logged in this window (%d events)." % rep["events"],
                "Log dir: %s" % rep["log_dir"]]
    L = []
    L.append("Fired on %d of %d turns (%s); %d pointers injected, %d of them from "
             "other sessions." % (
                 rep["fires"], rep["turns"], _pct(rep["fires"], rep["turns"]),
                 rep["pointers"], rep["pointer_origins"]["other_session"]))
    r = rep["reads"]
    if r.get("read_back_events"):
        L.append("Read back: %d of %d fires (%s) - by tier: strict %d, low-confidence %d, "
                 "foreign %d." % (
                     r["fires_with_a_read"], rep["fires"],
                     _pct(r["fires_with_a_read"], rep["fires"]),
                     r["by_tier"]["strict"], r["by_tier"]["loose"], r["by_tier"]["other"]))
    else:
        L.append("Read back: NOT MEASURED in this window - no tool-call attribution event "
                 "yet. Only live turns that call session_search produce one.")
    t = rep["tokens"]
    L.append("Cost: %d tokens total, %.0f per fire, %.0f per turn; %.0f ms per turn mean, "
             "%.0f ms worst. Turns with no pointer: %d." % (
                 t["total"], t["per_fire"], t["per_turn"],
                 rep["latency_ms"]["mean"], rep["latency_ms"]["max"],
                 sum(rep["miss_reasons"].values())))
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="verbatim-recall performance report")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--session")
    ap.add_argument("--detail", default="table",
                    choices=["table", "days", "sessions", "fires", "misses",
                             "terms", "all", "none"])
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    rep = vr.report(days=a.days, since=a.since, until=a.until, session=a.session)
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
        return 0 if rep["events"] else 3
    if not rep["events"]:
        print("No events in the window %s..%s (log dir %s holds other dates)."
              % (a.since or "start", a.until or "now", rep["log_dir"]))
        return 3

    win = rep["window"]
    span = "%s to %s" % (str(rep["first"])[:10], str(rep["last"])[:10])
    if win["since"] or win["until"]:
        span += " (asked %s..%s)" % (win["since"] or "-", win["until"] or "now")
    if win["session"]:
        span += " session %s" % win["session"]
    print("VERBATIM RECALL - %s  [%d events, %d log files]" % (
        span, rep["events"], rep["log_files"]))
    src = rep.get("sources") or {}
    if src.get("replay"):
        print("  NOTE: %d of %d events are a BACKFILL replay of real turns, not live "
              "gateway traffic." % (src["replay"], rep["events"]))
    other = {k: v for k, v in src.items() if k not in ("live", "replay")}
    if other:
        print("  NOTE: %d events carry a non-live src (%s); they are included in the "
              "totals below." % (sum(other.values()),
                                  ", ".join("%s=%d" % (k, v) for k, v in sorted(other.items()))))
    print()
    for line in headline(rep):
        print(" " + line)
    print()

    print("SUMMARY")
    print("\n".join(_table([
        ("turns", rep["turns"]),
        ("  events live / replay",
         "%d / %d" % ((rep.get("sources") or {}).get("live", 0),
                      (rep.get("sources") or {}).get("replay", 0))),
        ("fired", "%d (%s)" % (rep["fires"], _pct(rep["fires"], rep["turns"]))),
        ("announce-only", rep["announce_only"]),
        ("pointers", rep["pointers"]),
        ("  strict / low-confidence",
         "%d / %d" % (rep["pointer_tiers"]["strict"],
                      rep["pointer_tiers"]["low_confidence"])),
        ("  from the file corpus", rep["pointer_tiers"].get("files", 0)),
        ("  same / other session",
         "%d / %d" % (rep["pointer_origins"]["same_session"],
                      rep["pointer_origins"]["other_session"])),
        ("fires read back",
         "%d (%s)" % (rep["reads"]["fires_with_a_read"],
                      _pct(rep["reads"]["fires_with_a_read"], rep["fires"]))
         if rep["reads"].get("read_back_events") else "n/a - not measured"),
        ("unread fires", rep["reads"]["unread_fires"]
         if rep["reads"].get("read_back_events") else "n/a - not measured"),
        ("tokens total", rep["tokens"]["total"]),
        ("  per fire / per turn",
         "%s / %s" % (rep["tokens"]["per_fire"], rep["tokens"]["per_turn"])),
        ("latency mean / max ms",
         "%s / %s" % (rep["latency_ms"]["mean"], rep["latency_ms"]["max"])),
        ("searches with no pointer",
         rep["false_negative_candidates"]["searches_without_pointer"]),
        ("gated turns / candidates",
         "%d / %d" % (rep["false_negative_candidates"]["gated_turns"],
                      rep["false_negative_candidates"]["gated_candidates"])),
    ], [("metric", False), ("value", True)])))

    if rep["miss_reasons"]:
        print()
        print("WHY NO POINTER")
        print("\n".join(_table(
            [(k, v) for k, v in sorted(rep["miss_reasons"].items(),
                                       key=lambda kv: -kv[1])],
            [("reason", False), ("turns", True)])))

    det = a.detail
    want = lambda k: det in ("all", k) or (det == "table" and k == "days")

    if want("days"):
        print()
        print("PER DAY")
        print("\n".join(_table(
            [(r["day"], r["turns"], r["fires"], r["ptrs"], r["reads"])
             for r in rep["per_day"] if r["day"]],
            [("day", False), ("turns", True), ("fires", True),
             ("pointers", True), ("read", True)])))

    if det in ("all", "sessions"):
        print()
        print("OTHER SESSIONS USED")
        print("\n".join(_table([(s, n) for s, n in rep["other_sessions"]],
                               [("session", False), ("pointers", True)])))

    if det in ("all", "terms"):
        print()
        print("TOP MATCHED TERMS")
        print("\n".join(_table([(t, n) for t, n in rep["top_terms"]],
                               [("term", False), ("pointers", True)])))

    if det in ("all", "fires", "misses"):
        evs = vr.load_metrics(a.days, a.since, a.until)
        if a.session:
            evs = [e for e in evs
                   if not e.get("session") or e.get("session") == a.session]
        if det in ("all", "fires"):
            rows = []
            for e in [x for x in evs if x.get("ev") == "fire"][-a.limit:]:
                ptrs = e.get("ptrs") or []
                f = [p for p in ptrs if p.get("o") == e.get("session")]
                o = [p for p in ptrs if p.get("o") != e.get("session")]
                rows.append((
                    str(e.get("at"))[5:16],
                    str(e.get("session") or "")[-12:],
                    e.get("turn"),
                    "%d/%d" % (len(f), len(o)),
                    len({t for p in ptrs for t in (p.get("terms") or [])}),
                    e.get("tokens"),
                    round(float(e.get("ms") or 0)),
                ))
            print()
            print("FIRES (last %d)" % len(rows))
            print("\n".join(_table(
                rows, [("when", False), ("session", False), ("turn", True),
                       ("same/other", True), ("terms", True), ("tokens", True),
                       ("ms", True)])))
        if det in ("all", "misses"):
            rows = [(str(e.get("at"))[5:16], str(e.get("session") or "")[-12:],
                     e.get("turn"), e.get("why"),
                     (e.get("diag") or {}).get("best") or "")
                    for e in [x for x in evs if x.get("ev") == "miss"][-a.limit:]]
            print()
            print("NO-POINTER TURNS (last %d)" % len(rows))
            print("\n".join(_table(
                rows, [("when", False), ("session", False), ("turn", True),
                       ("reason", False), ("best candidate", False)])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
