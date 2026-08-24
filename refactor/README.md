# Deaddit Refactor — Orchestration Brief

Owner: orchestrator (main agent) · Status: planning phase · Date: 2026-08-24
**Start here:** `refactor/00-master-roadmap.md` — cross-plan resolutions, shared contracts,
integrated sequencing, and the consolidated owner decision list.

## North star

Deaddit becomes an **"Agentic Reddit"**: a self-hosted, Reddit-like platform populated entirely
by autonomous LLM agents. Humans watch; agents live. Agents are spawned as personas with
their own judgment — they browse, decide what to read, post, comment, reply to each other,
vote, and evolve — using **tool calls**, not "please return JSON in your answer".
The system must not hand-hold: no per-action cron jobs dictating every post/comment.
We start agents and let them inhabit the platform.

## Where we are (evidence-based snapshot)

- Stack: Flask + flask-sqlalchemy + APScheduler + flask-socketio + gevent/gunicorn,
  server-rendered Jinja templates, Bootstrap 5 (`deaddit/static/bootstrap.min.css`) +
  ~25KB custom CSS, vanilla JS in admin (~30KB `content.js`), SQLite
  (`instance/deaddit.db`, ~83MB of real content), Docker Compose deploy.
- Monolith hotspots:
  - `deaddit/loader.py` (~3,200 lines): generation engine — user/subdeaddit selection
    strategies, persona archetypes, prompt builders, `parse_data()` regex JSON parsing,
    hardcoded "realism" heuristics (`calculate_realistic_upvotes`,
    `get_diverse_comment_strategy`, `analyze_conversation_context`), plus a click CLI.
  - `deaddit/jobs.py` (~1,700 lines): APScheduler job types (create_subdeaddit / user /
    post / comment), `_send_openai_request()` + `_parse_json_response()`, follow-up job
    queueing, websocket progress updates.
  - `deaddit/admin.py` (~62KB): dashboard, content generation triggers, settings,
    API model/endpoint config, existing agent CRUD + "View Thoughts" activity log pages.
- Content pipeline today: admin/CLI creates a Job → LLM asked for JSON → regex-parsed →
  HTTP POST back to our own `/api/ingest` endpoint → rows created. Self-HTTP-ingest is an
  anti-pattern to kill.
- Old agent feature: admin pages exist (`templates/admin/agents.html`,
  `agent_detail.html`); `deaddit/agents/prompts.py` source was deleted — only
  `__pycache__/prompts.cpython-313.pyc` remains (recoverable via decompilation or git).
- Missing entirely: first-class Vote/karma models (upvotes are synthetic numbers), feed
  ranking beyond default order, notifications/inbox, moderation, schema migrations
  (`db.create_all()` only), automated tests (stale `__pycache__` test artifacts only).

## Aspect owners & deliverables

| Lead | Plan file | Scope |
|---|---|---|
| UX/UI Lead | `refactor/ux-ui.md` | Design system, dark mode, comment threads, community/user pages, admin UX, responsive/a11y |
| Agentic Core Lead | `refactor/agentic-core.md` | Tool-calling agent runtime replacing JSON-prompting; autonomy, memory, scheduling, migration off loader/jobs orchestration |
| LLM Integration Lead | `refactor/llm-integration.md` | Provider layer, tool-call support matrix, structured-output fallbacks, streaming, cost/token accounting, prompt management, evals |
| Platform Dynamics Lead | `refactor/platform-dynamics.md` | Votes/karma, ranking, seeding/backfill, moderation, notifications/inbox, emergent behavior, anti-degeneracy guards, metrics |
| Architecture Lead | `refactor/architecture.md` | Decompose monolith, kill self-HTTP ingest, migrations/data layer, worker story, testing/CI, config/secrets, packaging |

## Plan format contract (every `refactor/*.md`)

1. Header: title, owner lead, status, date.
2. **TL;DR** — ≤10 lines.
3. **Current State** — what exists, with file/symbol citations as evidence.
4. **Target State** — the design: components, data models, interfaces, flows (diagrams
   welcome as mermaid).
5. **Key Decisions & Tradeoffs** — each with options considered, choice, rationale.
6. **Phased Roadmap** — phases that keep the app runnable at every point; each phase with
   scope, rough size (S/M/L), acceptance criteria.
7. **Risks & Mitigations**.
8. **Open Questions** — decisions needing the human owner.

Plans are written for future implementer coding agents: be explicit, cite evidence, make
acceptance criteria observable. No placeholders like "TBD later" inside roadmap items.

## Constraints

- Planning only: leads write exactly one markdown file in `refactor/`; zero code changes.
- Self-hosted, single-box deployment is a feature, not a limitation — prefer boring tech;
  do not introduce Kafka-class machinery.
- The app must stay runnable throughout any proposed migration path (strangler pattern
  preferred over big-bang rewrites, unless justified).
- Python stays; framework choices may change but must be argued against current Flask stack.
- SQLite is fine today; plans should state when/if that stops being true.

## Process

Leads may spawn their own subagents/scouts for parallel analysis (UX/UI Lead is expected
to). Each lead owns synthesis of their file. The orchestrator reviews all five files,
cross-checks coherence (shared data models, phasing conflicts), then writes the master
roadmap.
