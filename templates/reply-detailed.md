## Is verbatim-recall helping? — {{window_first}} → {{window_last}}

**Verdict:** yes on forgetting and cost per recovered fact; evidence quality differs per
axis, so each line below carries how it is known — `MEASURED` (straight from the event
log), `DERIVED` (arithmetic on measured values, shown), `MODEL` (needs a stated
assumption), `NOT MEASURED` (cannot be observed here — never reported as zero).

### Scorecard

| Axis | What the number says | Compared with | How it is known |
|---|---|---|---|
| Accuracy | {{strict}}/{{ptrs}} pointers were exact-span hits ({{strict_pct}}%); {{loose}} were explicitly labelled low-confidence instead of being withheld | no recall at all = 0 pointers, so any hit is a gain; *wrong* hits are the risk, and they are labelled rather than silent | {{acc_label}} |
| Forgetfulness | {{forget_text}} | before recall, compressed turns were unreachable: 0 of them | {{forget_label}} |
| Rework | {{rework_text}} | a re-investigation repeats searches and re-reads whole files; a pointer costs {{tok_fire}} tokens | {{rework_label}} |
| Cost | {{cost_text}} | one full re-investigation ≈ {{rework_tokens}} tokens (assumption, `--rework-tokens`) | {{cost_label}} |

### 1. Accuracy — did the right thing come back?

{{acc_text}}. Tiering is the honesty mechanism: a **strict** pointer is a multi-term
span match, a **low-confidence** pointer is a partial match surfaced on purpose
(false positives are cheap to dismiss, false negatives are lost detail). The share of
low-confidence hits is the price being paid for recall, and it is {{loose_pct}}% of
pointers here.

*Not measured:* whether the agent actually used a pointer correctly, and whether the
answer it then gave was right. That needs read-back events (`ev: read`); this window
has {{read_back}}. Treat accuracy as **pointer quality**, not answer quality, until
read-back is populated.

### 2. Forgetfulness — what is recoverable that used to be lost?

{{store_turns}} turns of session `{{arch_session}}` are summarised away but kept verbatim;
the archived pointer region spans ids {{arch_lo}}–{{arch_hi}} ({{arch_size}} ids).
{{long_span_fires}} of {{fires}} fires pointed into a block that large, i.e. into
material the context had already summarised away.

Before recall, the compression boundary was destructive: everything above it was gone.
Now it is a cache miss, not a loss.

### 3. Rework — is the same ground being covered twice?

Measured proxies, because intent is not observable from a log:

- **Recurring topics:** {{repeat_topics}} distinct topics matched in two or more
  separate fires ({{top_repeat}}). Each recurrence is a chance to re-read instead of re-derive.
- **Cross-session pointers:** {{cross}} pointers came from *other* sessions — work this
  session never had in context at all, so it could not have avoided the rework any other way.

Full read-back telemetry is {{read_back}}, so the honest claim is "rework was made
avoidable", not "rework was avoided by N".

### 4. Cost — cheaper or just more tokens?

Measured: {{tokens_total}} tokens across {{fires}} fires, {{tok_turn}} per turn,
{{tok_fire}} per fire (max {{tok_max}}), latency {{ms_mean}} ms mean / {{ms_max}} ms max.

The model that makes those numbers mean something:

- one pointer ≈ {{tok_fire}} tokens; one full re-investigation ≈ {{rework_tokens}} tokens
- **break-even repeat-investigation rate = {{tok_turn}} / {{rework_tokens}} = {{breakeven_pct}}%**
- measured baseline without recall = {{baseline_pct}}%, so headroom = {{headroom_pct}} points
- recall only has to prevent **{{avoid_pct}}%** of the repeat investigations it fires on to pay for itself
- if it prevents all of them, net ≈ **{{net_tok_turn}} tokens per turn saved**; if it prevents
  none, the cost is a flat {{tok_turn}}-token/turn overhead — small, but not nothing

Sensitivity: halve `--rework-tokens` to 1500 and break-even doubles to ~{{tok_turn_1500}}%;
double it to 6000 and break-even halves to ~{{tok_turn_6000}}%. The verdict tracks the
assumption, which is why the assumption is printed.

### What would change the answer

- read-back rate low (< 20% of fires read) → pointers are being paid for and ignored; fix the pointer format, not the store
- low-confidence share rising with no rise in strict hits → retrieval regressing; the terms and gates need tuning
- repeat-topic count falling to the break-even rate → recall is buying little; revisit the assumption that re-investigation costs {{rework_tokens}} tokens

### Reproduce

```
python vr_value.py --level detailed --days 30        # this report
python vr_value.py --html report.html                # same, self-contained HTML
python vr_stats.py --detail fires                    # every fire, span and cost
python vr_stats.py --detail misses                   # why a turn got no pointer
python vr_stats.py --detail terms                    # top matched terms
```
