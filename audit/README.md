# Grok fact audit (offline; not part of the graded system)

`system/` makes every decision with fixed rules and no language model. This folder uses Grok (xAI)
**offline** to look for places where those rules read the free text wrongly. Grok never decides SAME or
DIFFERENT, and nothing here runs when `system/run` runs.

| Stage | Question | How |
|---|---|---|
| 1 `stage1.py` | Did we clear a hit on a misread fact? | For each DIFFERENT decision, Grok reads the raw sources blind and returns facts with an exact quote. Code drops any quote not in its source. (a) Grok's facts are added to ours and **our own rules** decide again; a clear that no longer clears is a finding. (b) The fact a clear rests on is checked against Grok's reading. |
| 2 `stage2.py` | Would new wording make us clear a hit we should not? | Grok rewrites the free text behind every REVIEW/SAME hit, keeping the facts. Our unchanged engine runs on the reworded copy; any hit that becomes DIFFERENT is a would-be unsafe clear. |

Design choices:
- **Quote or drop.** Every fact carries a source ID and a verbatim quote, checked by code, so an invented fact cannot reach a finding and a person checks one sentence, not a record.
- **Blind.** Grok never sees our extracted facts, so it cannot just agree with them.
- **Cached.** Every answer is stored by (model, prompt version, prompt) in `audit/cache/`. Re-runs are free and give identical results. The cache and `audit/out/` hold dev data and are git-ignored.
- **Pinned model:** `grok-4.20-0309-non-reasoning`, temperature 0.

Run: `python audit/stage1.py <data folder>` and `python audit/stage2.py <data folder>`. The key comes from
`XAI_API_KEY` or the file in `XAI_KEY_FILE` (default `~/.secrets/xai_key.txt`).
