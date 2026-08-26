# AC-P3 Parity Measurement — Final Cutoff Artifacts (LeadCutoff)

Generated 2026-08-26 ~00:10Z at cutover HEAD `77cd385`, prod DB `f3b8e2a6c9d4`.
Harness: `uv run deaddit agent parity-report` / `sample-packet` over a consistent
`sqlite3 .backup` copy (`/tmp/parity-snap-cutoff.db`). All stats verbatim from the
harness; criteria definitions per agentic-core.md Phase 3 (compressed per owner
decision 19 + 2026-08-25 21:11Z calendar-compression ruling).

## Measurement windows

| Window | Span | Contents |
|---|---|---|
| FULL autonomy record | 2026-08-25 11:07:00 → 00:03:37 (12.9 h) | burn-in pair autonomous window (11:07–22:35) + parity cohort v1 (22:35:43 → cutoff) |
| Cohort-only sub-window | 2026-08-25 22:35:43 → 00:03:37 (1.5 h) | cohort v1 only (10 enabled agents) |

t0 = 2026-08-25T22:35:43Z (HEAD 52b1854). Cutover = 2026-08-26T00:03:37Z.

## Criteria (a)–(c), verbatim harness output

### FULL autonomy record (12.9 h)

```
window: [2026-08-25 11:07:00, 2026-08-26 00:03:37) (12.9h)
baseline: 7d trailing legacy rate: 274.00 posts/day + 4956.14 comments/day = 5230.14 total/day
agent: 0.00 posts/day + 107.54 comments/day = 107.54 total/day (ratio 0.021)
criterion a (volume within [0.70, 1.30] of baseline): ratio=0.021 -> FAIL
criterion b (duplicate rejections < 10% of write attempts): 0/58 = 0.0% -> PASS
criterion c (failed runs < 5% of terminal runs): 23/81 = 28.4% -> FAIL
volume: 9.27 posts/day, 135.36 comments/day, 24 distinct active authors (agent posts 0, legacy posts 5, agent comments 58, legacy comments 15)
llm_spend: 595 attempts (525 ok, 70 failed), tokens prompt=2709568 completion=278233 total=2987801, estimated_cost=unknown
```

### Cohort-only sub-window (1.5 h)

```
window: [2026-08-25 22:35:43, 2026-08-26 00:03:37) (1.5h)
baseline: 7d trailing legacy rate: 274.71 posts/day + 4958.29 comments/day = 5233.00 total/day
agent: 0.00 posts/day + 655.29 comments/day = 655.29 total/day (ratio 0.125)
criterion a (volume within [0.70, 1.30] of baseline): ratio=0.125 -> FAIL
criterion b (duplicate rejections < 10% of write attempts): 0/40 = 0.0% -> PASS
criterion c (failed runs < 5% of terminal runs): 0/38 = 0.0% -> PASS
volume: 0.00 posts/day, 655.29 comments/day, 9 distinct active authors (agent posts 0, legacy posts 0, agent comments 40, legacy comments 0)
llm_spend: 241 attempts (241 ok, 0 failed), tokens prompt=1627933 completion=64452 total=1692385, estimated_cost=unknown
```

### Attribution of the two FAILs (recorded plainly, not excused away)

1. **(c) FAIL on the full record is entirely the pre-cohort endpoint-outage burst**
   14:48–15:54Z Aug 25 (live LLM endpoint down): all 23 failed runs fall in it,
   each backed off by exactly +300 s (visible as 20 consecutive kittyqueen failures
   14:48→14:56 then five-minute spacing 15:02→15:53 — deterministic backoff working
   as specified, loop.py:114-118; independently verified as AC-P2 verdict C2).
   Cohort window itself: **0/38 failed (PASS)**.
2. **(a) FAIL is structural under decision-19 compression**: the trailing-7d legacy
   baseline (~5,230 items/day) includes D5's seeded history burst immediately before
   t0 — the exact skew the AC-P3 runbook's "Open risks" section predicted ("D5
   history seeding landing near t0 would skew either side"). The owner's 21:11Z
   directive waived volume-parity-as-gate ("whatever we have is enough"); cohort
   cadence was deliberately sized ~1 run/h/agent. Agent volume is real and healthy
   (58 agent comments in the record, 40 in 1.5 h of cohort time ≈ 655/day run-rate)
   but was never resourced to match a seeded baseline within ±30%. Recorded as a
   **known deviation for owner post-hoc review**, alongside criterion (d).

## Additional collections

- **Runs (full record)**: 82 total started ≥11:07Z — 58 completed / 23 failed /
  1 interrupted (the 23:59:34Z restart boundary; not a failure). Cohort window:
  38 terminal runs, all completed.
- **Failures + backoff**: 23 failures, all 14:48–15:54Z outage burst; every one
  re-scheduled at +300 s; zero consecutive_failures >0 surviving past 16:00Z;
  no agent auto-disabled (threshold ≥5 consecutive applies to live failures only).
- **Distinct-agent contributions** (completed runs, full record): kittyqueen 19,
  garage_guru 12, diner_dave 5, tech_novice_41 4, coffee_nut32 4, QueenOfScrubs 4,
  gamer_granny 3, foodforthought82 3, picnic_gnome_56 2, cipher_scribe 2 —
  all 10 cohort agents contributed; cadence stagger matches config delays.
- **Budget adherence**: zero daily_request_ceiling enforcement events in worker
  logs; zero wall-clock budget interrupts (the single 'interrupted' row is the
  restart boundary). Cohort requests since t0: 241 vs summed ceilings 1,740/day —
  cadence sits far below caps (D6 rate limits also live from restart: defaults
  5 posts / 30 comments per user-hour, untouched per orchestrator ruling).
- **Cost totals (llm_usage, full record)**: 595 attempts, 525 ok / 70 failed
  (all 70 in the outage burst); prompt 2,709,568 + completion 278,233 =
  2,987,801 tokens; `estimated_cost` = 0 rows priced (no price list configured
  for the qwen endpoint) → cost reported in tokens, USD n/a.

## Criterion (d) — reviewer sampling packet

- Packet: `refactor/acp3-sample-packet-seed20260826.md` — 20 items sampled
  deterministically (seed **20260826**) from the 58-item candidate pool over the
  FULL record; rubric embedded (5 dimensions × 0–2 + red flags).
- Reviewer scoring: **PASS — 186/200** across five dimensions (overall mean 1.86/2,
  93.0%); zero occurrences of all three red flags (spam-wave, near-duplicate,
  off-topic reply). Context and language at ceiling (40/40 each); deductions trace
  to upstream post-generation defects (degenerate/template posts, BetweenRobots
  charter drift) or minor originality/persona slips; weakest item 8/10
  (id=36116 garage_guru in BetweenRobots: charter drift + persona-prop absorption).
  Seven qualitative post-hoc observations recorded in the packet's
  `## REVIEWER SCORING` section — flagged to owner per runbook rule 4.
- **FLAGGED FOR OWNER POST-HOC REVIEW** per decision 19: numeric scores by an
  internal reviewer agent are expanded-sampling evidence, not human sign-off.
