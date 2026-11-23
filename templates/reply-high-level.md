**Yes — the forgetting is now reversible, and the cost is measurable.** {{fires}} of {{turns}} turns got a pointer ({{fire_pct}}%), {{ptrs}} pointers total, {{tok_turn}} tokens/turn.

- **Accuracy — {{acc_label}}:** {{acc_text}}.
- **Forgetfulness — {{forget_label}}:** {{forget_text}}.
- **Rework — {{rework_label}}:** {{rework_text}}.
- **Cost — {{cost_label}}:** {{cost_text}}.

Break-even is {{breakeven_pct}}% repeat investigation against a measured {{baseline_pct}}% baseline, so recall has to prevent **{{avoid_pct}}%** of the repeat investigations it fires on to pay for itself — a demanding bar, not a formality; if it prevents none, it is a flat {{tok_turn}}-token/turn overhead.

Window {{window_first}} → {{window_last}} · {{live}} live / {{replay}} replay rows{{replay_note}} · detail: `python vr_stats.py --detail fires` · numbers: `python vr_value.py --level detailed`
