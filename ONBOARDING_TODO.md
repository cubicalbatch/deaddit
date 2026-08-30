# Onboarding TODO — first-startup experience on a fresh DB

Walkthrough date: 2026-08-29, worktree `deaddit-onboarding` (branch `onboarding-fresh-start`),
fresh `instance/onboarding.db`, dev server on a random port, mock OpenAI-compatible
endpoint for the LLM. Everything below was reproduced by driving the real UI.

Goal per owner: first startup guides the user through all base setup, and the end
result is **agents configured and creating content with minimal friction**.

## Verified working today

- Fresh DB serves the "Setup Required" wizard at `/` (empty DB + unconfigured).
- `Load default data` loads 19 subdeaddits + 50 users correctly.
- Persona generator (`Agents` page) with "auto-create autonomous agent" creates
  enabled fixed-persona agents with `next_run_at=now` — works against the
  Config-level `OPENAI_API_URL` even with zero `LLMProvider` rows.
- Once `AGENT_RUNTIME_ENABLED=true` and `deaddit-worker` is running, queued
  force-runs execute, agents complete visits, and posts appear on the feed.
  The pipeline itself is fine — every gap below is guidance/discoverability.

## Issues found (ordered by severity)

### 1. The wizard self-destructs after ANY single step — CRITICAL
`routes.py::index` gates the wizard on `empty DB AND not is_configured`.
Saving only the API URL (step 1, listed first and marked "Required") flips
`is_configured` → the wizard disappears and `/` becomes an empty feed
("Nothing here but crickets"). The same happens if data is loaded first
(`has_content` → gone). The "Setup Complete!" branch is unreachable dead code.
**Fix:** gate the wizard on "DB still empty" only (config-independent), and add
a permanent `/admin/setup` route so the wizard is always reachable from admin.

### 2. "Load default data" becomes unreachable from the UI — CRITICAL
`admin.load_default_data_api` is referenced ONLY by `setup.html`. After the
wizard vanishes (issue 1) there is no admin page that loads default data; the
endpoint is orphaned. README claims default data is loadable "via the setup
flow or `/admin`" — false today.
**Fix:** permanent Setup page (see 1) keeps the step; dashboard checklist links
to it.

### 3. No UI for `AGENT_RUNTIME_ENABLED` — CRITICAL
The flag is env/Setting-only; no template mentions it. The wizard's parting
note says "enable agents on the Agents page", but the Agents page has no such
control. Agents show `next_run_at` timestamps that will never fire; a Force Run
leaves the agent `queued` forever with zero feedback.
**Fix:** runtime toggle on the Agents page + Setup "Go live" step writing the
Setting (worker already re-reads it each poll; no restart needed).

### 4. No signal that `deaddit-worker` must be running — CRITICAL
Web and worker are separate processes; a fresh user (especially non-Docker)
has no idea. Admin shows queued jobs and next-run times that silently never
execute. `WORKER_HEARTBEAT_AT` Setting (written by the worker) is never shown.
**Fix:** worker-liveness line (last heartbeat age) on the Agents page and Setup;
include the exact start command (`uv run deaddit-worker` / `make worker`).

### 5. Wizard names the wrong env var — BUG
`setup.html` help text says keys go in `OPENAI_API_KEY`; the code reads
`OPENAI_KEY` / `API_KEY_<ENDPOINT>` (`.env.example` uses `OPENAI_KEY`).
A user following the hint sets the wrong name and gets auth failures.
**Fix:** correct the name; also show live key-status for the entered endpoint.

### 6. Wizard never asks for the model — HIGH
Default `OPENAI_MODEL=llama3` is used blindly by generated agents (the
generator form has no model field). Against most real endpoints every request
fails with an obscure 404/400.
**Fix:** model field in the wizard step 1; show resolved default.

### 7. Two parallel LLM config systems confuse the flow — MEDIUM
Wizard writes legacy `Config.OPENAI_API_URL`; Settings page pushes
`LLMProvider` rows ("No LLM providers saved yet"); the Create-Agent form's
provider dropdown reads "No providers configured" even when the wizard URL
works. Nothing tells the user which is authoritative (answer: either works;
providers are per-agent overrides).
**Fix:** wizard step 1 notes the relationship in one sentence; Settings page
mentions that agents fall back to the default endpoint when no provider is set.

### 8. No first-run checklist on the dashboard — MEDIUM
Dashboard shows zeros but never tells you what to do next. The first thing a
fresh admin sees is a "set an API_TOKEN" security warning — inverted priorities
(and the token is env-only anyway).
**Fix:** onboarding checklist card on the dashboard: LLM configured → starter
data loaded → agents registered → runtime enabled → worker alive; each item
with a deep link; card collapses when all green. Security warning stays but
after/secondary to setup.

### 9. Wizard has no connection test — LOW
Capabilities page can probe endpoints, but the wizard never links it and has
no inline test; misconfigurations surface much later as agent failures.
**Fix:** "Test connection" button in step 1 reusing the existing probe
endpoint, verdict inline.

### 10. README/doc drift — LOW
- "`/admin`" default-data claim (false — see 2).
- Env var naming consistent with `.env.example` only after fixing 5.
**Fix:** README first-startup section pointing at the Setup page.

## Implementation plan (subagent dispatch)

Wave 1 (parallel, disjoint files):
- **A. Wizard + backend** (`routes.py`, `templates/setup.html`, `admin.py`,
  `static/style.css`): issues 1, 2, 3(backend), 4(backend), 5, 6, 7(note), 9.
  Adds `GET /admin/setup`, `GET /admin/api/setup/status`,
  `POST /admin/api/agents/runtime`, extends `save_config_api` to accept model,
  wizard steps 1–4 with live status refresh.
- **D. Docs** (`README.md`): issue 10.

Wave 2 (parallel, disjoint files):
- **B. Agents page + dashboard** (`templates/admin/agents.html`,
  `templates/admin/dashboard.html`): issues 3(UI), 4(UI), 8 — banners, toggle,
  liveness, checklist card consuming wave-1 endpoints.
- **C. Tests** (`tests/`): gating change, new endpoints' contracts, runtime
  toggle effect.

Integration: verify full fresh-DB walkthrough in the browser again, `make
lint`, `make test`, commit on `onboarding-fresh-start`.

## Status — IMPLEMENTED (2026-08-29, branch onboarding-fresh-start)

All items fixed and verified end-to-end with a fresh DB + mock LLM endpoint:

- [x] 1–2: `/` wizard now gated on empty-DB only (survives partial steps);
      permanent `/admin/setup` page; "Load default data" always reachable.
- [x] 3: runtime toggle in wizard step 4 + Agents page banner
      (`POST /admin/api/agents/runtime` writes the Setting; worker re-reads
      per poll — no restart needed).
- [x] 4: worker liveness (`WORKER_HEARTBEAT_AT`, 90s threshold) shown in the
      wizard, Agents page banner, and dashboard checklist; includes the exact
      start command.
- [x] 5: env var corrected to `OPENAI_KEY` / `API_KEY_<ENDPOINT>`; live
      key-status line for the saved endpoint.
- [x] 6: model field in wizard step 1 (saved via `openai_model`).
- [x] 7: wizard notes provider/Config relationship.
- [x] 8: dashboard "Get started" checklist card, auto-hides when complete.
- [x] 9: "Test connection" button (tools-capability probe, inline verdict,
      key redacted from errors).
- [x] 10: README first-startup section corrected.

New endpoints: `GET /admin/setup`, `GET /admin/api/setup/status`,
`POST /admin/api/agents/runtime`, `POST /admin/api/setup/test-connection`.
Tests: `tests/test_onboarding_setup.py` (7 tests). Full suite green
(1396 passed). Verified in-browser: fresh DB → wizard → test connection →
save URL+model → load data → 3 agents → enable runtime → start worker →
"Setup complete — agents are live" → posts on the feed from scheduled wakes.
