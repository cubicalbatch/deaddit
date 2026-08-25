# Refactor % Done — monitor

Owner-facing progress estimate. Updated by the orchestrator at every forward step
(phase closed, milestone verdict). Method kept simple per owner instruction:
count-based on tracked steps, with a size-weighted figure (S=1/M=2/L=3) because
remaining phases are heavier than finished ones.

**Current: ~12% done** (size-weighted 7/61) · raw count 8/35 steps closed (~23%)
As of: 2026-08-24 20:27 · LeadA3 running (Wave 2 open)

Why the two numbers diverge: Waves 0–1 (5 small/mid phases) are closed, but the
heavy tail — A5 worker, D1–D3 dynamics, AC P3 parity burn-in (longest wall-clock
item), Wave 6 deletions — is still ahead. Expect the weighted number to lag the
raw count until Wave 4 starts closing.

Wall-clock caveat: task-count % ignores time; the ≥24 h parity window alone will
occupy a large share of remaining calendar time at a fixed %-point.

## Log

- 2026-08-24 20:27 — Monitor seeded. Closed so far: A0 be4f9c3, UX-0 498255d,
  LLM-1 deeb753, A1 8c12505, A2 5854af0 + this run's preflight (dirty-file audit,
  pre-A3 DB copy verified md5 cb4c9528…f27, ledger repair). In flight: LeadA3.
