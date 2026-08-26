# Final-Gate Verification Report — clean clone CI, offline eval, deletion greps, compose

Executor: LeadFinalGate · Date: 2026-08-26 · Cutover base sha: `77cd385` · Final sha: **`121b96d`**
Scope: completion-checklist items that do NOT touch prod. All execution in isolated
clones under /tmp (`/tmp/finalgate-clone`, `/tmp/finalgate-final`) plus one explicitly
authorized new file (this report). Zero prod contact; nothing pushed.

---

## 0. Verdict summary

| # | Checklist item | Verdict |
|---|---|---|
| 1 | Clean clone: `uv sync && uv run pytest && uv run ruff check .` green | **PASS** (after one test-only date-rot fix, see §2) |
| 2 | Eval suite offline run green; regression report stored | **PASS with documented gap** (planned `deaddit/llm/evals/` harness never implemented; offline parity harness executed instead — §3) |
| 3 | Deletion greps (AgenticCore P4 + architecture ledger) zero at final HEAD | **PASS** (§4) |
| 4a | `docker compose config` validates | **PASS** (§5) |
| 4b | `docker compose up -d --build` web+worker healthy | **PASS — executed live**, not merely cited from TesterA6 (§5) |

## 1. Shas and delta discipline

- All cutover/prod legs ran against `77cd38529657a91bf019df9104c5e03b35b9e137`.
- Closeout gate found ONE failing test at that sha (§2). Owner-approved (Main,
  2026-08-26) single-file fix committed as
  `121b96d8746b65f63cdf6bf67762f88f6df7dec8`
  ("refactor(D6): stamp rollup fixture created_at to pinned day (date-rot)").
- The delta `77cd385..121b96d` is **tests-only** (`tests/test_d6_metrics.py`, +9/−3):
  no schema, no production code, no migration change. Single alembic head remains
  `f3b8e2a6c9d4`; prod validity at `f3b8e2a6c9d4` is unaffected.

## 2. Clean-clone CI

Procedure (fresh-machine simulation; network only for package resolution):

```
git clone /home/loki/git/deaddit /tmp/finalgate-final   # commits only; no working-tree bleed
cd /tmp/finalgate-final && git rev-parse HEAD           # 121b96d8746b65f63cdf6bf67762f88f6df7dec8
uv sync          # exit 0
uv run pytest    # 693 passed, 1 skipped in 98.56s  (694 collected)
uv run ruff check .   # "All checks passed!"
```

### 2a. Defect found and fixed: calendar-coupled D6 rollup test

At `77cd385`, the suite was RED:
`tests/test_d6_metrics.py::TestRollupDay::test_every_column_matches_hand_computed_fixture`
— `AssertionError: assert {'agent': 0, ... 'seed': 0} == {'agent': 1, ... 'seed': 1}`.

Root cause (confirmed by code reading + reproduction): the fixture creates Post/Comment
rows WITHOUT pinning `created_at`, so they inherit the column default
`datetime.utcnow` (`models.py:33`). The test asserts provenance for `_DAY = date(2026,8,25)`
via `metrics._provenance()`, which filters `Post.created_at >= start AND < end`. The test
therefore passed only while "now" was still inside 2026-08-25 UTC and rotted exactly at
2026-08-26T00:00:00Z — hours after D6's tester verdict (which ran before midnight UTC).

Fix: stamp all six fixture content rows with explicit `created_at=_dt(hour=…)` matching
their ActivityEvent timestamps. Test intent unchanged; file-scoped verification 11/11
passed; full suite re-run green twice consecutively.

Evidence trail: initial red runs at 00:07–00:16 UTC captured `1 failed, 692 passed,
1 skipped`; post-fix runs `693 passed, 1 skipped` (×2 independent full-suite executions).
Note: the very first background run's `PYTEST_EXIT=0` was an artifact of piping pytest
through `tail` (exit code of `tail`, not pytest) — caught and corrected during this gate.

## 3. Offline eval suite

### 3a. Gap: planned LLM Phase-6 eval harness does not exist

`llm-integration.md §Eval harness` specifies `deaddit/llm/evals/` runnable as
`python -m deaddit.llm.evals --suite personas` with fixtures, deterministic scorers
(tool-arg validity rate, simhash duplicate rate), JSON regression reports wired into CI.
**This package was never implemented**: no `deaddit/llm/evals/` at HEAD, no such module
invocation possible, and no LLM-6 phase row exists in PROGRESS.md's executed-phase table
(the integrated roadmap never scheduled it into a wave). The gap is therefore a plan-vs-
execution omission to record in the owner's final report, not a broken artifact.

### 3b. Offline eval portion that DOES exist and ran green

The de-facto offline regression instruments at HEAD:

1. **FakeProvider deterministic suite** — every deterministic LLM test routes through
   `tests/fakes.py FakeProvider` (zero network): covered by the 693-passed clean-clone run.
   Scoped evidence: `uv run pytest tests/test_acp3_parity_harness.py tests/test_llm_fake.py`
   → **23 passed**.
2. **AC-P3 offline parity measurement harness** (`deaddit/agents/parity.py`: pure-SQL,
   read-only, no ORM/network/LLM) exercised end-to-end via its CLI against a seeded
   throwaway SQLite DB (schema per `tests/test_acp3_parity_harness.py`; seeded: 7-day
   legacy baseline 15 items/day, 24h agent window at ratio 1.13, 1 dup-rejection of
   25 write attempts, 20/20 completed runs, 30 ok llm_usage rows):

```
$ uv run deaddit agent parity-report --db /tmp/finalgate-eval/scratch.db \
    --window-start "2026-08-24 00:00:00" --window-end "2026-08-25 00:00:00" \
    --baseline-days 7 --json     # exit 0; raw JSON captured
window: [2026-08-24 00:00:00, 2026-08-25 00:00:00) (24.0h)
baseline: 7d trailing legacy rate: 15.00 total/day
agent: 17.00 total/day (ratio 1.133)
criterion a (volume within [0.70, 1.30]): ratio=1.133 -> PASS
criterion b (duplicate rejections < 10%): 1/25 = 4.0% -> PASS
criterion c (failed runs < 5%): 0/20 = 0.0% -> PASS
```

All three computable criteria evaluate correctly offline; criterion (c) thresholds and
the write-attempt/read-tool distinction verified by the hand-computed fixtures in
`tests/test_acp3_parity_harness.py`.

## 4. Deletion-grep re-verification at final HEAD

Executed in the clean clone of `121b96d` (`deaddit/templates`+`deaddit/static` are inside
`deaddit/`; planning docs in `refactor/` are historical record and excluded per ledger
intent). Exit 1 = zero matches.

### Ledger 1 — agentic-core.md Phase-4 acceptance grep

```
$ grep -rnE "_parse_json_response|_send_openai_request|_queue_comment_jobs|\
calculate_realistic_upvotes|/api/ingest" deaddit/
→ zero hits (exit 1)
```

### Ledger 1b — agentic-core.md D4 loader orchestration/heuristics purge

```
$ grep -rnE "create_post_with_replies|generate_comments_for_post|\
get_diverse_comment_strategy|analyze_conversation_context|get_varied_comment_structure|\
select_reply_target|get_dynamic_temperature|_execute_create_post|_execute_create_comment" deaddit/
→ zero hits (exit 1)
```

### Ledger 2 — architecture.md DELETION ledger (greppable rows)

```
$ grep -rnE "requests\.(post|put)\(" deaddit/            # self-HTTP ingest
→ zero hits (exit 1)   [sole remaining requests.post mention is prose in llm/transport.py:3 docstring]
$ grep -rnE "create_all" deaddit/                         # db.create_all()
→ zero hits (exit 1)
$ grep -rnE "^from .+ import \*|^import \*" deaddit/      # star imports
→ zero hits (exit 1)
$ grep -rnE "^\s*(import gevent|from gevent|import loguru|from loguru)" deaddit/ tests/
→ zero hits (exit 1)   [case-insensitive sweep hits are 'gEvent' inside vendored htmx.min.js
                        and a loguru mention in logging_config.py docstring history note]
$ grep -rnE "\bloader\b" deaddit/ tests/
→ only docstrings/comments + tests/test_no_self_http.py guard asserting
  `import deaddit.loader` RAISES (deleted-wholesale proof); module gone
```

### Rename & head checks

```
$ grep -rn "upvote_count" deaddit/                       → 0 hits
$ grep -rln "upvote_count" migrations/versions/ tests/
→ ONLY: migrations/versions/359878740bb0_baseline_schema.py (baseline),
        migrations/versions/c7e2a9b4d1f6_resolution4_upvote_count_to_score.py (the rename itself),
        tests/test_acp4_rename_migration.py (raw-SQL rename test)
  (+ their untracked __pycache__ bytecode; plus historical mentions confined to refactor/*.md/json)
```

Migration graph computed over all 15 revision files in the clean clone:
heads = `['f3b8e2a6c9d4']` — **single alembic head confirmed**.

## 5. Docker path (executed at final HEAD, fresh clone build)

```
$ docker compose config                                   → exit 0, valid
$ DEADDIT_WEB_PORT=5877 docker compose up -d --build      → exit 0
finalgate-clone-web-1    Up 43 seconds (healthy)
finalgate-clone-worker-1 Up 37 seconds (healthy)
$ curl -o /dev/null -w '%{http_code}' http://127.0.0.1:5877/   → 200
$ docker compose down -v                                  → clean teardown
```

This supersedes the fallback plan of citing TesterA6's replay at `4e50dd3`; for the
record, HEAD delta since `4e50dd3` is 9 commits (A6 README fidelity, AC-P4 rename,
UX-POST contrast passes, UX-6 live ticker/thought-log ×3, AC-P2 closure, D6) — all
individually tester-verified, and the compose image at final HEAD builds and goes
healthy from scratch.

## 6. Residual notes for the owner

1. **LLM Phase-6 eval harness unbuilt** (§3a): the roadmap's definition-of-done phrase
   "eval regression gates" is currently discharged by the FakeProvider suite + AC-P3
   parity harness. If prompt iteration continues post-refactor, the fixtures/scorers
   design in llm-integration.md remains the spec.
2. **Date-coupled tests**: the D6 rot is fixed, but the pattern (fixtures relying on
   `created_at=utcnow` while asserting pinned windows) warrants a linting eye if more
   rollup-window tests are added.
3. Warnings volume (~61k, dominated by a `datetime.utcnow()` DeprecationWarning in
   `llm/prompts.py:101`) is cosmetic and out of scope here.
