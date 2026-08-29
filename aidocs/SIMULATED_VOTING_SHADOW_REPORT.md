# Simulated-voting shadow preset comparison

**Report schema:** 1  
**Fixture seed:** `20260828`  
**Mode:** `shadow` (the direct `run_active_tick` entry point with `dry_run=True`)  
**Clock:** pinned UTC origin `2026-08-25 00:00:00`; ticks at +48h, +48h15m, +49h, +54h, +72h, +96h, +120h, and +144h  
**Fixture:** 24 personas, 4 communities, 48 posts, 96 ordinary comments, and one deterministic late comment that exposes an old parent thread for revival work. Worker bounds were used (`global_limit=100`, `per_item_limit=2`, archive limit 5, revival thread limit 50).

This is a scratch-DB comparison, not a write to `instance/deaddit.db`. Active proposals are de-duplicated by `(target, ordinal)` across repeated shadow ticks; tail opportunities are de-duplicated by their deterministic target/mode/ordinal identity. The comparison therefore measures the eventual proposal distribution rather than counting repeated observations of the same due ordinal.

## Results

The count distribution is across all 48 posts and 97 comments. Latency is the deterministic arrival offset from content creation (the tail uses the exposure time). HHI is the sum of squared shares; lower concentration is less concentrated. `changed/overlap` is changed positions / shared IDs in the final top-10 compared with the zero-score fixture at the final pinned time. Rising has no candidates in the final 24-hour window, so its comparison is intentionally `0/0`.

| Preset | Unique proposals | Count p50/p90/p95/p99 | Zero-vote fraction | Latency p50/p90/p95 | Upvote share | Subscription affinity | Persona cadence p50/p90/max | Community HHI / top share | Author HHI / top share | Karma p10/p50/p90 | Hot changed/overlap | Top changed/overlap |
|---|---:|---|---:|---|---:|---:|---|---|---|---|---|---|
| Quiet | 50 | 0 / 2 / 2 / 2 | 0.793103 | 3.00h / 72.00h / 72.00h | 0.960000 | 0.720000 | 2 / 3 / 3 | 0.263200 / 0.320000 | 0.063200 / 0.080000 | 0 / 2 / 4 | 9 / 10 | 10 / 5 |
| **Natural** | **63** | **0 / 2 / 2 / 2** | **0.772414** | **32.63m / 48.00h / 48.00h** | **0.888889** | **0.714286** | **3 / 4 / 4** | **0.255228 / 0.285714** | **0.057193 / 0.095238** | **0 / 2 / 4** | **4 / 10** | **8 / 9** |
| Busy | 58 | 0 / 2 / 2 / 2 | 0.786207 | 8.67m / 45.00m / 36.00h | 0.879310 | 0.724138 | 2 / 3 / 4 | 0.253270 / 0.275862 | 0.057075 / 0.068966 | 0 / 2 / 4 | 1 / 9 | 10 / 0 |

| Preset | Active proposals (all ticks) | Archive proposals | Revival proposals | Echo/persona HHI | Max target share (brigading proxy) | Dissent/downvote share |
|---|---:|---:|---:|---:|---:|---:|
| Quiet | 435 | 15 | 1 | 0.047200 | 0.040000 | 0.040000 |
| **Natural** | **1,311** | **20** | **1** | **0.045100** | **0.031746** | **0.111111** |
| Busy | 2,885 | 27 | 1 | 0.044590 | 0.034483 | 0.120690 |

## Selection conclusion

Natural remains the recommended starting preset based on this fixture, without changing canonical values:

- It is the middle operating point for active proposal volume (1,311 versus 435 Quiet and 2,885 Busy), while producing the largest useful unique proposal set (63 versus 50 and 58).
- Its p50 arrival latency (32.63 minutes) is materially faster than Quiet (3 hours) while avoiding Busy's 8.67-minute front-loading; its p90 remains a bounded 48 hours.
- Its 0.888889 upvote share is between Quiet's 0.960000 and Busy’s 0.879310, and its dissent signal is correspondingly non-zero without dominating the fixture.
- Its community HHI is lower than Quiet and close to Busy, while author HHI remains low. Persona cadence stays bounded (p90 4, max 4), and subscription affinity is stable across presets (0.714–0.724).
- It changes fewer Hot and Top positions than either extreme (4/10 Hot and 8/9 Top), preserving a more stable ranking surface. The ranking result is not a quality judgment; it is a bounded-change observation from this zero-score fixture.
- Every preset exercised archive and revival paths (`archive_proposals > 0`, `revival_proposals = 1`); no preset values were tuned from this single fixture.

## Reproduction and determinism evidence

The harness seeded fresh SQLite databases from the same explicit fixture recipe and seed, then ran the same pinned clock and direct engine entry point for Quiet, Natural, and Busy. Two complete runs produced byte-identical JSON:

```text
4eaa314edac4e1ef774ec376bc9c4269019dc6acb0d051d0b2c2ef76f4a22e05  report4.json
4eaa314edac4e1ef774ec376bc9c4269019dc6acb0d051d0b2c2ef76f4a22e05  report5.json
```

The temporary harness and scratch databases were removed after capture. The permanent baseline comparison remains `deaddit dynamics baseline-report`; this report is only the preset/engine shadow comparison and does not duplicate that CLI.
