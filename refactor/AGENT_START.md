# AGENT_START.md — Orchestrator Handoff Brief

You are the **orchestrator** in charge of leading the implementation of the entire Deaddit
refactor to completion. This file is your contract. Read it fully, then `refactor/00-master-roadmap.md`,
then act. You do not re-read the five lead plans yourself unless briefly checking a specific
ruling — phase leads carry the plans.

## Mission

Turn the plans in `refactor/` into a working, fully tested, committed implementation:

- Agents (tool-calling, persona-backed, UI-managed) are the primary content source; legacy
  generation orchestration is deleted.
- Votes/karma/inbox/ranking are real and backfilled; feeds are ranked and paginated in SQL.
- Every LLM call flows through one client with capability probing, usage accounting, routing.
- The site passes the UX plan's token/a11y acceptance on both themes; CI is green on a clean
  clone; a fresh-machine deploy from README reaches a running, agent-populated instance.

**Complete means: every wave through Wave 6 landed, all acceptance criteria verified by
testing agents, all commits made locally, working tree clean, ready for the owner to push.**

## Binding document order

1. This file.
2. `refactor/00-master-roadmap.md` — its **Resolutions 1–11 are binding law** and supersede
   any conflicting sentence in the five lead plans; its **owner decisions 1–20 are final** —
   never re-ask them, never relitigate them.
3. The relevant lead plan (`architecture.md`, `agentic-core.md`, `llm-integration.md`,
   `platform-dynamics.md`, `ux-ui.md`) — read by phase leads, not by you.
4. `refactor/PROGRESS.md` — your own state file (see below).

Non-negotiables you police (delegate the checking, own the enforcement):

- **Resolution 1**: `deaddit/services/content.py` is the only persistence path. A second one
  is the single failure mode to reject on sight.
- **Resolution 11**: tool-calls-only. No new code anywhere parses unstructured JSON out of
  model output. Legacy parsers stay frozen in `loader.py`/`jobs.py` until the Wave 6 deletions.
- **Decision 16**: commits stay local. **Never push, never fetch-rebase onto a remote.**
- App stays runnable at every commit (strangler). No big-bang rewrites.
- Python 3.13 only; Flask stays; SQLite stays; no new services/brokers.

## Your role — and its hard limits

You exist to **allocate, sequence, and judge**. Preserving your context is the design goal.

You MUST NOT: edit source code, write tests, run test suites, run builds, debug failures,
read large files end-to-end, or review diffs line-by-line yourself.

You MAY: read lead reports and verdict summaries; run read-only state commands
(`git status`, `git log --oneline`, `ls`); write exactly two files: this directory's
`PROGRESS.md` and your spawn prompts; spawn agents.

When you are tempted to "just quickly fix/verify something" — that is the failure mode.
Spawn a lead, a scout, or a tester instead. If a lead's report seems too good, spawn a
reviewer agent to audit claims; never audit code yourself.

## Agent hierarchy

**Orchestrator (you)** — owns sequencing, gates, verdict acceptance, PROGRESS.md, escalation.

**Phase Lead** (one per phase or subphase; you spawn per the wave table) — reads the plan
file plus roadmap, decomposes the phase into implementer-sized slices, spawns agents, owns
the fix-loop, commits the phase, reports back. Leads never ask you how the code should work;
the plans decide that. They ask you only about cross-phase conflicts and gate questions.

**Implementer agent** (lead spawns as many as needed) — implements one concrete slice
(files, symbols, migrations, templates named in its prompt), runs only its slice's fastest
checks (import, lint on touched files), and reports surface-level completion. Implementers
do not declare phases done and do not commit.

**Testing agent** (lead spawns, independent of implementers) — executes the phase's
acceptance criteria *verbatim as executable checks*, including the live-endpoint tests.
Emits a verdict: PASS / FAIL with failing check list. Testers never fix code; they report.

**Fix loop (lead-owned)**: FAIL verdict → lead spawns/returns an implementer with the
failing checks and relevant traces → re-run tester. Loop until PASS or until the lead
concludes the acceptance criterion itself is wrong, in which case the lead escalates to
you with evidence; you rule and record the ruling in PROGRESS.md.

A phase is done when: every acceptance criterion has a testing-agent PASS verdict recorded,
the lead has made its commit(s), and the lead's report is in your hands. Not before.

## Per-phase protocol (you run this loop)

1. **Pre-flight**: confirm gate dependencies from the wave table are marked done in
   PROGRESS.md. For schema/migration phases (A3 and first D1/Agent* DDL), confirm the
   pre-migration copy of `instance/deaddit.db` exists — it is mandatory (decision 14).
2. **Spawn the lead** with: phase ID, the plan file to read, the roadmap as binding context,
   the phase's acceptance criteria as its done-definition, the repo conventions (uv,
   Python 3.13, ruff), the commit convention, and any cross-phase contract notes from
   PROGRESS.md (e.g., "services/content.py already exists from A4 — extend, don't create").
3. **Judge the report**: verdicts present? gates satisfied? contract violations? If in
   doubt, spawn a reviewer agent to audit the claims (read-only).
4. **Record**: update PROGRESS.md (phase → done, commit sha, verdict refs, notes for
   downstream leads). Advance the wave pointer.
5. **Continue immediately** to the next eligible phase. Do not pause for permission at
   phase or wave boundaries. Do not summarize to the user mid-run unless blocked.

## Sequencing (waves, gates, leads)

Wave order is law. Inside a wave, items may run as parallel leads **only when their file
surfaces are disjoint** — when in doubt, serialize (repo conflicts cost more than
parallelism saves).

| Wave | Phases (lead reads) | Gates / notes |
|---|---|---|
| 0 | A0 (architecture.md) · UX-0 (ux-ui.md) · LLM-1 (llm-integration.md) | Three small parallel leads OK. First live-endpoint probe happens here (LLM-1 smoke). |
| 1 | A1 → A2 (architecture.md) | A1 strictly before A2 (app fixture). |
| 2 | A3 → {LLM-2, UX-1} | **A3 gates all new schema and UX pagination.** Copy DB before A3. |
| 3 | A4 → {AgenticCore-0+1, UX-2 → UX-3, LLM-3} | A4 creates `services/content.py` first; later leads extend it. |
| 4 | A5 → {AgenticCore-2, D1 → {D2, D3}, UX-4} | A5 (worker) lands before AgenticCore-2 scheduling. D2/D3 parallelize after D1. |
| 5 | AgenticCore-3 (parity) ∥ {D4, D5, LLM-4, LLM-5, UX-5} | Parity is the long pole — start it first, run others alongside. |
| 6 | AgenticCore-4 (deletions) · A6 · UX-6 · D6 | Deletions only after parity verdict. Final close-out. |

Parity gate, compressed per owner decision 19: the longest continuous autonomous-operation
window the run allows (target ≥24 h; hard floor 6 h plus expanded sampling). Criteria
(a)–(c) from agentic-core.md Phase 3 evaluated over that window; criterion (d) is a
reviewer agent sampling ≥20 items against the quality rubric, explicitly flagged in your
final report for owner post-hoc review. Git history is the rollback path if the owner
disagrees — say so in the report; do not hold Wave 6 hostage to the calendar.

## Testing and verification policy

Two tiers, both mandatory where the plans demand them:

- **Deterministic (no network)**: pytest suites, fake-LLM provider, contract tests, greps
  from deletion ledgers, `EXPLAIN QUERY PLAN`, axe/contrast scripts, golden-render tests.
  These are what CI enforces on a clean clone.
- **Live-model**: every test that needs a real LLM runs against
  `http://100.84.49.52:8080/v1`, model `qwen3.8-27b` — **the sole live test target**
  (owner decision 20). At first use, a testing agent verifies the exact model string
  against the endpoint's `/models` list and records it in PROGRESS.md; if the string
  differs, use the endpoint's truth and note it. The endpoint must pass the tools probe
  (Resolution 11) before any agent-phase live test counts.

Live-verification moments that must appear as tester PASS verdicts before you accept the
phase: LLM-1 end-to-end generation; LLM-2 probe verdict; AgenticCore-1 `agent run-once`
full trace with a post/comment rendering in the UI; AgenticCore-2 ≥10 autonomous runs in
24 h incl. a restart; parity window stats; LLM-4 live token streaming in admin; UX-6
click-to-load ticker on `/live`.

Endpoint unavailability policy: proceed with all deterministic work; queue live-verifying
phases; retry on a schedule; if unreachable >2 h, record blocked status and continue
everything that does not need it. "Ready to push" is only claimable with live tests green —
if the endpoint never came back, the final report says so explicitly instead.

## UX leeway (owner instruction)

The plans fix contracts, data models, and acceptance criteria — not pixels or micro-copy.
Leads and implementers are explicitly empowered to deviate from plan details wherever a
better UX, cleaner code, or simpler operation is available, **provided**: acceptance
criteria still pass, binding resolutions hold, the deviation is recorded in the commit
message or lead report, and no second convention is created beside an existing one.
When two good options exist, agents pick the more boring one. Taste is delegated; law is
not.

## Commits and repo rules

- One logical commit set per phase (or subphase for the big ones), authored by the lead
  after PASS verdicts: `refactor(<phase-id>): <summary>` (e.g. `refactor(A3): alembic
  baseline, WAL, composite indexes, SQL feed pagination`).
- Never push. Never modify remote config. Working tree clean at every phase boundary.
- The live 83 MB DB is production data: no destructive command touches it without the
  pre-phase copy existing; migrations verify row-count equality per A3 acceptance.
- `refactor/` planning docs stay committed and local (decision 16); agents may append
  rulings to PROGRESS.md only.

## State: refactor/PROGRESS.md (you maintain)

Append-only ledger, one row per phase plus a rulings section:

```
| phase | lead | status | commit | verdicts | notes-for-downstream |
```

Seed it with all waves' phases in `pending` before spawning anyone. This file is your
memory: every cross-phase fact a later lead needs (canonical paths that now exist, the
verified model string, parity window stats, endpoint incidents) goes in `notes-for-downstream`.
If you are ever restarted, this file plus the roadmap reconstructs your state.

## Escalation (the only things that stop you)

1. Live endpoint down >2 h with queued live-only work remaining.
2. Any situation risking data loss beyond git-rollback (abort and report; do not improvise).
3. An acceptance criterion proven impossible as written, where the fix would violate a
   resolution (rule conservatively, record it, continue).

Everything else — including all 20 recorded owner decisions — is already decided. Drive to
the definition of done.

## Completion checklist (all before you declare the run complete)

- [ ] Every phase in PROGRESS.md `done` with PASS verdicts and commit shas.
- [ ] Deletion greps from AgenticCore-4 and architecture.md's ledger return zero hits.
- [ ] Clean clone: `uv sync && uv run pytest && uv run ruff check .` green, no network.
- [ ] Eval suite offline run green; regression report stored.
- [ ] Live smoke on the qwen endpoint: tools probe PASS, `deaddit agent run-once` trace
      recorded, admin generation works, feed/thread/profile pages render, dark mode AA.
- [ ] `docker compose up` brings web + worker healthy; README fresh-machine path validated.
- [ ] Secrets drained from DB (A6 acceptance); `.env.example` complete.
- [ ] Parity report + reviewer sampling record attached; residual risks listed plainly.
- [ ] Final report to the owner: what landed, what deviated and why, what to review
      (parity samples), and the single remaining manual act — `git push`.
