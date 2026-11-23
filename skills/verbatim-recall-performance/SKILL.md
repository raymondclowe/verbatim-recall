---
name: verbatim-recall-performance
description: Use when asked how verbatim recall is helping. Report headline, table, drill-down.
---

# verbatim recall: is it helping?

## Answer shape (do not deviate)

1. **One-line headline** — the single number that answers the question.
2. **Detail table** — the small set of aggregates, one per line.
3. **Drill-down** — only if asked, or if a number is clearly wrong.

No preamble, no method narration, no restating the request.

## Run this first

```bash
python <repo>/vr_stats.py --days 30
```

Exit `0` = data present, exit `3` = log empty or nothing in that window (then say so plainly;
do **not** substitute a different number).

Drill-downs, one per question:

```bash
python <repo>/vr_stats.py --days 30 --detail days       # per-day trend
python <repo>/vr_stats.py --days 30 --detail sessions   # which sessions fire
python <repo>/vr_stats.py --days 30 --detail fires      # each fire + its pointers
python <repo>/vr_stats.py --days 30 --detail misses     # silent turns + why
python <repo>/vr_stats.py --days 30 --detail terms      # most-matched query words
python <repo>/vr_stats.py --days 30 --json              # raw aggregate
```

Log: `<hermes home>/logs/verbatim-recall/events-YYYY-MM-DD.jsonl`, one JSON line per turn.
Knobs are YAML under `plugins.entries.verbatim-recall.settings.metrics.*` — `enabled`, `path`,
`retention_days`, `store_terms`, `shadow_knobs`. Never tell the user to set an environment
variable; this plugin has no env-var knobs by design.

## What the numbers mean

- **fires / turns** — firing rate. High is not automatically good.
- **pointers** — ids injected. `foreign` = from other sessions (cross-session recall).
- **read back** — the only metric that shows the pointers were *used*.
- **tokens** — cost per turn; ~100–250 tokens is a cheap pointer, thousands is a paragraph.
- **strict vs low-confidence** — precision tiers; low-confidence hits are deliberate
  (false positives are accepted over false negatives) and must be reported separately.
- **no-pointer turns** — the gate correctly stayed silent.

## Two honesty rules, enforced

1. If the report prints a **replay NOTE**, every row in that window came from `vr_replay.py`:
   real messages through the real gate, but **not live traffic**. Say so before any number —
   a replay cannot prove the feature helps in production.
2. **`n/a - not measured` is not `0%`.** Read-back is unmeasurable in a replay, and invisible
   for reads that bypass `session_search`. Report it as unmeasured and name the reason.

## Reading the result

- Firing ~100% with read-back unmeasured or ~0 → pointers are being injected and ignored;
  that is the defect to fix. Do not present the firing rate as evidence of value.
- `foreign` 0 across many sessions → cross-session recall is not firing; check
  `cross_session.sources` and `same_user_only`.
- Mean latency above a few hundred ms → the index is being rebuilt per turn; check the
  region cache rather than tuning the gate.

## Before blaming the numbers

Plugin modules are imported once at gateway start. If the log shows no rows while the
pointers are visibly being injected, the running process predates the metrics code — restart
the gateway (this drops live sessions, so ask first), then re-run. Verify the deployed copy
matches the repo instead of assuming: compare bytes.
