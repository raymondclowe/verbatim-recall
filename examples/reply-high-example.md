**Yes — the forgetting is now reversible, and the cost is measurable.** 70 of 78 turns got a pointer (90%), 95 pointers total, 96.1 tokens/turn.

- **Accuracy — MEASURED:** 39 of 95 pointers were exact-span hits (41%); 51 were labelled low-confidence rather than dropped.
- **Forgetfulness — MEASURED:** 2328 turns of session 20261112_172849_f7171a9f are summarised away but still readable verbatim (ids 19235-26333); 5 of 70 fires pointed into a block that large.
- **Rework — DERIVED:** 29 topics recurred across separate fires (proxy for re-investigation); 6 pointers came from other sessions.
- **Cost — MEASURED + MODEL:** 96.1 tokens per turn, 58.3 ms per turn; break-even is 3.2% repeat investigation at 3000 tokens each.

Break-even is 3.2% repeat investigation against a measured 5.3% baseline, so recall has to prevent **60.4%** of the repeat investigations it fires on to pay for itself — a demanding bar, not a formality; if it prevents none, it is a flat 96.1-token/turn overhead.

Window 2026-11-23T12:31 → 2026-11-23T13:20 · 18 live / 60 replay rows · 60 of 78 rows are a backfill replay, not live traffic · detail: `python vr_stats.py --detail fires` · numbers: `python vr_value.py --level detailed`

