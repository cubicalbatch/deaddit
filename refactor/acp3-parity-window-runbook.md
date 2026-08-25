# AC-P3 Parity Window Runbook — INPUT (LeadACP3)

Status: scaffolding committed · **window NOT activated** · t0 activation happens only at the
Wave-4 cutover (~11:40Z Aug 26) by the AC-P2 closeout lead. This file is the runbook INPUT
requested from the AC-P3 brief; the orchestrator folds it into the cutover runbook.

Gate being implemented (owner decision 19): the longest continuous autonomous-operation
window this run allows — target ≥24 h, hard floor 6 h plus expanded sampling. Criteria
(a)–(c) of agentic-core.md Phase 3 are evaluated over that window; criterion (d) is a
reviewer-agent sampling of ≥20 produced items, explicitly flagged for owner post-hoc
review. Git history is the rollback path — Wave 6 is not held hostage to the calendar.

## Surfaces built (this phase)

| Artifact | Path | Purpose |
|---|---|---|
| Cohort spec | `deaddit/agents/parity_cohort.json` | 10 personas (8–15 band), tiers regular/power_user/lurker, staggered cadence bounds, `daily_request_ceiling` budget keys |
| Cohort validation + creation | `deaddit/agents/cohort.py`, `deaddit agent create-cohort` | one gated command; tools-probe fires once before any write; upsert semantics; memory backfill per owner decision 4 |
| Measurement harness | `deaddit/agents/parity.py`, `deaddit agent parity-report` | criteria (a)–(c) + context stats + LLM spend, pure SQL over ANY db copy |
| Sampling packet | `deaddit agent sample-packet` | seeded/deterministic ≥20-item packet with rubric scoring sheet |

## t0-readiness checklist (all must hold before activation)

1. AC-P2 burn-in verdict recorded (≥10 autonomous runs incl. restart leg) and worker
   restarted by the closeout lead (`AGENT_RUNTIME_ENABLED` stays `true` — it was flipped
   during burn-in; do NOT toggle it again).
2. Prod migrations `d2c4f8a16e90` + `e5d7f9a1c3b9` applied under md5-ledgered snapshot,
   EQP/row-count sanity green (closeout lead's steps, listed here as ordering dependencies).
3. Live endpoint probe green: `http://100.84.49.52:8080/v1`, model `qwen3.8-27b`
   reverified vs `/models` (the create-cohort probe does exactly this and refuses to write
   otherwise — Resolution 11 gate).
4. Repo head = AC-P3 commit; working tree clean; app boots.
5. Legacy generation still runnable (D7 throttle-not-freeze): admin batch counts dialed
   down so total volume stays flat while provenance mix shifts. This dial-down is an
   admin-UI act by the closeout lead, not a code change.

## t0 activation procedure (exact commands)

Run from repo root on the refactor branch. The cohort command is an UPSERT: personas
`kittyqueen` and `garage_guru` already exist as burn-in agents (ids 2/3, enabled) and get
their configs normalized; the other 8 are created fresh. All rows land disabled unless
`--enable`; backfill (decision 4) runs per persona with a deterministic extractive
fallback if the endpoint hiccups.
```bash
# 1. Force a FRESH capability probe (owner decision 2: re-verify endpoint/model
#    at cohort creation). ensure_tools_allowed short-circuits on a cached
#    verdict, so drop the cached row first; the next command re-probes live:
uv run python -c "from deaddit import create_app; \
from deaddit.extensions import db; \
from deaddit.models import EndpointCapability; \
app=create_app(); app.app_context().push(); \
cap=db.session.get(EndpointCapability, ('http://100.84.49.52:8080/v1','qwen3.8-27b')); \
db.session.delete(cap); db.session.commit()"

# 2. Create + activate the cohort in ONE gated command (probe first, then writes):
uv run deaddit agent --db <PROD_DB_URI> create-cohort \
    --spec deaddit/agents/parity_cohort.json --enable

# Expected output: 10 agent lines + 'Cohort v1: 10 agents ...' summary +
# 'probe evidence: {...}' JSON line (finish_reason=tool_calls, validated args).
# Non-zero exit = probe or spec failure; NOTHING written.

# 3. Confirm state:
uv run deaddit agent list

# 4. Stamp t0: record date -u +%Y-%m-%dT%H:%M:%SZ, git rev-parse HEAD, and
#    `uv run deaddit agent list` output into the cutover log. t0 = the timestamp here.
```

Notes:

- If the probe fails: fix endpoint/model config, rerun. No partial writes occur before a
  green probe (gate precedes all writes).
- If a persona-backfill warns, proceed — episodes fall back to extractive summaries and
  can be re-run later (`backfill_persona_history` is idempotent, returns 0 when rows exist).
- Do NOT pass `--no-backfill-memory` at t0 unless the endpoint is degraded; owner decision 4
  requires personality+history inheritance.

## Mid-window rules

1. **HEAD sha at every worker restart.** Code evolution mid-window IS allowed (run
   precedent), but every worker restart logs `git rev-parse HEAD` + UTC timestamp into the
   cutover/parity log BEFORE the restart. No gratuitous restarts.
2. **No prompt-behavior flips** without hub coordination with Main and the affected leads
   (LLM-5 infra may land anytime; flipping LIVE cohort prompts waits until the window
   closes — standing Wave-5 contract 4).
3. **Measurement cadence**: at least once every 24 h, run the harness against a THROWAWAY
   copy of the prod DB (`cp instance/deaddit.db /tmp/parity-snap-<ts>.db` then point the
   command at the copy):

   ```bash
   uv run deaddit agent parity-report --db /tmp/parity-snap-<ts>.db \
       --window-start <t0> --window-end <now> --baseline-days 7
   uv run deaddit agent sample-packet --db /tmp/parity-snap-<ts>.db \
       --seed 20260826 -n 20 -o /tmp/parity-sample-packet.md
   ```

   Read-only queries against the live file are technically safe (`mode=ro`), but copies
   give a consistent snapshot and honor prod discipline.
4. **Criterion (d)**: hand the generated packet to a REVIEWER agent; it scores all items
   on the attached rubric (5 dimensions × 0–2 + red flags) and returns PASS/concerns. The
   scored packet goes into the final report EXPLICITLY FLAGGED for owner post-hoc review —
   it is not a self-standing sign-off.
5. **Failure handling**: runtime self-guards stay active (consecutive_failures ≥ 5 →
   auto-disable; failure backoff 300 s; rate caps; duplicate suppression). A disabled
   agent may be re-enabled manually after inspecting `deaddit agent list` +
   View-Thoughts traces; note every manual intervention in the parity log.
6. **Rollback**: agents off = `deaddit agent disable <username>` per agent (or leave the
   window to expire); legacy pipeline remains runnable throughout (strangler). Git history
   is the rollback path for code.

## End-of-window verdict

With t0..t1 covering the achieved continuous window (target ≥24 h, floor 6 h):

- (a) `criterion_a.pass == True` — agent posts+comments/day within ±30% of trailing legacy
  baseline (indeterminate if baseline is zero → escalate to Main, do not fake).
- (b) `criterion_b.pass == True` — duplicate-suppression rejections <10% of write attempts.
- (c) `criterion_c.pass == True` — failed runs <5% of terminal runs.
- (d) reviewer rubric sheet complete over ≥20 samples, flagged for owner post-hoc review;
  spam-wave phrasing / near-duplicates / non-contextual replies = concerns to record even
  on numeric PASS.

Verdict + stats go to Main for PROGRESS.md; AgenticCore P4 deletions remain gated on it.

## Open risks

- Baseline definition uses trailing non-agent daily mean immediately before the window;
  D5 history seeding landing near t0 would skew either side — coordinate seeding vs t0
  stamp ordering with LeadD5/Main (seeding writes carry non-agent provenance and inflate
  the baseline if inside the baseline span).
- 6 h floor vs 24 h target: if the run's remaining calendar forces ≤24 h, expanded sampling
  (larger `-n`) compensates per decision 19; say so plainly in the final report.
- Burn-in agents kittyqueen/garage_guru join the cohort with normalized configs; their
  pre-t0 runs pollute nothing (window starts at t0) but their enable-state changes at t0.
- qwen `nothink` quirk applies (LLM prefill adapter) — watch turn latency, not correctness.
