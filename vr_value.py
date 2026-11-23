#!/usr/bin/env python3
"""vr_value — turn the verbatim-recall event log into a value report.

vr_stats answers "what did the plugin do?".  This answers the question a user
actually asks: "is it helping me — better accuracy, less forgetting, less
rework, lower cost?"  Every number is labelled with how it was obtained:

    MEASURED   straight out of the event log
    DERIVED    arithmetic on measured numbers (the arithmetic is printed)
    MODEL      needs an assumption; the assumption is printed alongside
    NOT MEASURED  cannot be observed from this data — printed as such, never 0

    python vr_value.py                                  # high-level markdown
    python vr_value.py --level detailed                 # full markdown report
    python vr_value.py --html report.html               # self-contained HTML
    python vr_value.py --days 7 --rework-tokens 5000    # change one assumption
    python vr_value.py --json                           # the numbers, no prose

Exit 0 with data, 3 when the window has no events.
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "templates")


def _default_log_dir() -> str:
    """The plugin's own resolver, so this tool and vr_stats cannot disagree.

    They did: this file hardcoded ~/.local/share/hermes/logs/verbatim-recall while
    vr_stats asked the plugin (which resolves HERMES_HOME, hence the profile dir).
    On a profile install vr_stats reported 17 events while vr_value said "No events
    in the window" -- and vr_value is the tool that answers "is it worth it".
    """
    try:
        sys.path.insert(0, HERE)
        import verbatim_recall as _vr
        return _vr.metrics_dir()
    except Exception:
        return os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"),
            "hermes", "logs", "verbatim-recall")


DEFAULT_LOG_DIR = _default_log_dir()


# ---------------------------------------------------------------- data loading

def load_events(log_dir, days=None, since=None, until=None, session=None):
    files = sorted(glob.glob(os.path.join(log_dir, "events-*.jsonl")))
    since = since or (datetime.now() - timedelta(days=days or 30)).strftime("%Y-%m-%d")
    rows = []
    for f in files:
        for line in open(f, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            at = str(r.get("at") or "")[:10]
            if at and (at < since or (until and at > until)):
                continue
            if session and r.get("session") != session:
                continue
            rows.append(r)
    return rows, len(files)


def summarise(rows, rework_tokens=3000, baseline_repeat_pct=5.3):
    """All numbers the report needs, each tagged with its provenance."""
    fires = [r for r in rows if r.get("ev") == "fire"]
    misses = [r for r in rows if r.get("ev") == "miss"]
    reads = [r for r in rows if r.get("ev") == "read"]

    ptrs = [p for r in fires for p in (r.get("ptrs") or [])]
    tier = Counter(p.get("tier") or "?" for p in ptrs)
    cross = [p for p in ptrs if p.get("o") and p.get("o") != "same" and
             p.get("cross")]
    cross_count = sum(1 for r in fires for p in (r.get("ptrs") or []) if p.get("o") != r.get("session"))

    terms = Counter()
    for r in fires:
        for t in dict.fromkeys(r.get("terms") or []):
            terms[t] += 1
    repeat_topics = {t: n for t, n in terms.items() if n >= 2}

    long_spans = [r for r in fires
                  if any((p.get("hi", 0) - p.get("lo", 0)) >= 99 for p in (r.get("ptrs") or []))]
    announce = [p for p in ptrs if p.get("tier") == "announce"]
    best = max(announce, key=lambda p: p.get("hi", 0) - p.get("lo", 0), default={})
    arch_lo = best.get("lo", 0)
    arch_hi = best.get("hi", 0)
    arch_session = best.get("o", "-")

    toks = [r.get("tokens", 0) for r in fires] or [0]
    ms = [r.get("ms", 0) for r in fires if r.get("ms")]
    turns = len(rows)
    tok_total = sum(toks)
    tok_turn = round(tok_total / turns, 1) if turns else 0
    n = max(len(fires), 1)

    # ---- cost model: pointer cost per turn vs one avoided re-investigation
    breakeven_pct = round(100.0 * tok_turn / rework_tokens, 2) if rework_tokens else 0
    headroom_pct = round(baseline_repeat_pct - breakeven_pct, 2)
    avoid_pct = round(100.0 * breakeven_pct / baseline_repeat_pct, 1) if baseline_repeat_pct else 0
    net_tok_turn = round(baseline_repeat_pct / 100.0 * rework_tokens - tok_turn, 1)

    src = Counter(r.get("src") or "live" for r in rows)
    return {
        "window_first": min((str(r.get("at") or "") for r in rows), default="-")[:16],
        "window_last": max((str(r.get("at") or "") for r in rows), default="-")[:16],
        "turns": turns,
        "live": src.get("live", 0),
        "replay": src.get("replay", 0),
        "fires": len(fires),
        "misses": len(misses),
        "fire_pct": round(100.0 * len(fires) / turns) if turns else 0,
        "ptrs": len(ptrs),
        "strict": tier.get("strict", 0),
        "loose": tier.get("loose", 0) + tier.get("low_confidence", 0),
        "announce": tier.get("announce", 0),
        "strict_pct": round(100.0 * tier.get("strict", 0) / len(ptrs)) if ptrs else 0,
        "loose_pct": round(100.0 * (tier.get("loose", 0) + tier.get("announce", 0)) / len(ptrs)) if ptrs else 0,
        "cross": cross_count,
        "cross_sessions": dict(Counter(p.get("o") for p in ptrs if p.get("o") and p.get("tier"))),
        "store_turns": max((r.get("turns") or 0 for r in rows), default=0),
        "arch_lo": arch_lo,
        "arch_hi": arch_hi,
        "arch_session": arch_session,
        "arch_size": max(0, arch_hi - arch_lo + 1),
        "long_span_fires": len(long_spans),
        "repeat_topics": len(repeat_topics),
        "top_repeat": sorted(repeat_topics.items(), key=lambda kv: -kv[1])[:6],
        "tokens_total": tok_total,
        "tok_turn": tok_turn,
        "tok_fire": round(tok_total / n, 1),
        "tok_max": max(toks),
        "ms_mean": round(sum(ms) / len(ms), 1) if ms else 0,
        "ms_max": round(max(ms), 1) if ms else 0,
        "read_events": len(reads),
        "read_back": "n/a - not measured" if not reads else str(len(reads)),
        "rework_tokens": rework_tokens,
        "baseline_pct": baseline_repeat_pct,
        "breakeven_pct": breakeven_pct,
        "headroom_pct": headroom_pct,
        "avoid_pct": avoid_pct,
        "net_tok_turn": net_tok_turn,
        "tok_turn_1500": round(100.0 * tok_turn / 1500, 2),
        "tok_turn_6000": round(100.0 * tok_turn / 6000, 2),
        "log_dir": os.path.dirname(glob.glob(os.path.join(DEFAULT_LOG_DIR, "events-*.jsonl"))[0]) if glob.glob(os.path.join(DEFAULT_LOG_DIR, "events-*.jsonl")) else DEFAULT_LOG_DIR,
    }


def verdicts(d):
    """One plain sentence per value axis, each saying how it is known."""
    acc = ("MEASURED" if d["ptrs"] else "NOT MEASURED",
           "%d of %d pointers were exact-span hits (%d%%); %d were labelled low-confidence "
           "rather than dropped" % (d["strict"], d["ptrs"], d["strict_pct"], d["loose"])
           if d["ptrs"] else "no pointers in the window")
    forget = ("MEASURED" if d["arch_size"] else "NOT MEASURED",
              "%s turns of session %s are summarised away but still readable verbatim "
              "(ids %d-%d); %d of %d fires pointed into a block that large"
              % (d["store_turns"] or "-", d["arch_session"], d["arch_lo"], d["arch_hi"],
                 d["long_span_fires"], d["fires"] or 0)
              if d["arch_size"] else "no archived-span pointer in the window")
    rework = ("DERIVED",
              "%d topics recurred across separate fires (proxy for re-investigation); "
              "%d pointers came from other sessions" % (d["repeat_topics"], d["cross"]))
    cost = ("MEASURED + MODEL",
            "%s tokens per turn, %s ms per turn; break-even is %s%% repeat investigation "
            "at %s tokens each" % (d["tok_turn"], d["ms_mean"], d["breakeven_pct"],
                                   d["rework_tokens"]))
    return {"accuracy": acc, "forgetfulness": forget, "rework": rework, "cost": cost}


# ------------------------------------------------------------------- rendering

def fill(text, d, v):
    """Substitute {{key}} placeholders."""
    badge = {"MEASURED": "b-ok", "DERIVED": "b-ok", "MEASURED + MODEL": "b-model",
             "MODEL": "b-model", "NOT MEASURED": "b-none"}
    total = d["ptrs"] or 1
    nums = {k: val for k, val in d.items() if not isinstance(val, (dict, list))}
    nums.update({
        "acc_label": v["accuracy"][0], "acc_text": v["accuracy"][1],
        "forget_label": v["forgetfulness"][0], "forget_text": v["forgetfulness"][1],
        "rework_label": v["rework"][0], "rework_text": v["rework"][1],
        "cost_label": v["cost"][0], "cost_text": v["cost"][1],
        "acc_badge_class": badge.get(v["accuracy"][0], "b-none"),
        "forget_badge_class": badge.get(v["forgetfulness"][0], "b-none"),
        "rework_badge_class": badge.get(v["rework"][0], "b-none"),
        "cost_badge_class": badge.get(v["cost"][0], "b-none"),
        "strict_width": round(100.0 * d["strict"] / total, 1),
        "loose_width": round(100.0 * d["loose"] / total, 1),
        "announce_width": round(100.0 * d["announce"] / total, 1),
        "replay_note": (" · %d of %d rows are a backfill replay, not live traffic"
                        % (d["replay"], d["turns"])) if d["replay"] else "",
        "top_repeat": ", ".join("%s×%d" % (t, n) for t, n in d["top_repeat"]) or "none",
    })
    out = text
    for k, val in nums.items():
        out = out.replace("{{%s}}" % k, str(val))
    return out


def render(level, d, v):
    name = "reply-detailed.md" if level == "detailed" else "reply-high-level.md"
    path = os.path.join(TEMPLATES, name)
    body = fill(open(path, encoding="utf-8").read(), d, v)
    if level == "detailed" and d["replay"]:
        body = ("> NOTE: %d of %d rows in this window come from `vr_replay.py` — real messages "
                "through the real gate, but **not live gateway traffic**. Read every number "
                "below as a backfill.\n\n" % (d["replay"], d["turns"])) + body
    return body


def render_html(d, v):
    return fill(open(os.path.join(TEMPLATES, "value-report.html"), encoding="utf-8").read(), d, v)


def main(argv=None):
    ap = argparse.ArgumentParser(description="verbatim-recall value report")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--session")
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    ap.add_argument("--level", choices=["high", "detailed"], default="high")
    ap.add_argument("--html", metavar="PATH", help="write a self-contained HTML report")
    ap.add_argument("--rework-tokens", type=int, default=3000,
                    help="assumed token cost of one full re-investigation (default 3000)")
    ap.add_argument("--baseline-repeat-pct", type=float, default=5.3,
                    help="measured repeat-investigation rate without recall (default 5.3)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    rows, nfiles = load_events(a.log_dir, a.days, a.since, a.until, a.session)
    if not rows:
        print("No events in the window (log dir %s)." % a.log_dir)
        return 3
    d = summarise(rows, a.rework_tokens, a.baseline_repeat_pct)
    v = verdicts(d)

    if a.json:
        print(json.dumps({"numbers": d, "verdicts": {k: list(x) for k, x in v.items()}},
                         indent=2, default=str))
    if a.html:
        open(a.html, "w", encoding="utf-8").write(render_html(d, v))
        print("wrote %s" % a.html)
    if not a.json and not a.html:
        print(render(a.level, d, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
