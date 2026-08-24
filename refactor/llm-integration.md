# Deaddit Refactor — LLM Integration Layer

Owner: LLM Integration Lead (`llm-integration.md`) · Status: draft for orchestrator review · Date: 2026-08-24

## TL;DR

- Today there are **two independent, diverged OpenAI-compatible HTTP clients**: `deaddit/loader.py::send_request()` (with retries/backoff, persona-driven sampling) and `deaddit/jobs.py::_send_openai_request()` (no retries, different defaults). Both hand-build payloads, both duplicate a `stop`-value hack list, and both rely on regex JSON scraping (`parse_data()`, `_parse_json_response()`).
- We replace them with one internal package `deaddit/llm/`: a single client module, a **tool-calls-only capability probe** (endpoints/models without reliable native tool calling fail fast — no `json_mode`, no salvage, ever), unified retry/timeout policy, streaming events piped to the existing flask-socketio admin namespace, per-request token/cost accounting rows, DB-routed model selection, and versioned prompt templates.
- Self-hosted-first remains a constraint, but the 2026 baseline assumes tool-trained models (Qwen3-class, Llama 3.1+) on modern serving stacks (vLLM, llama.cpp `--jinja`, LM Studio, Ollama's OpenAI shim): capabilities are *probed and cached per endpoint*, never assumed — and a failed tools probe is a verdict, not a fallback trigger (owner ruling 2026-08-24, roadmap Resolution 11).
- Phase 1 is a pure consolidation win (one client, one retry policy, no behavior change beyond added retries on the jobs path) that lands safely before any agentic work.

## Current State

### Two duplicated clients

**`deaddit/loader.py::send_request(system_prompt, prompt, user_personality_traits, content_type)` (lines 747–905)**

- Hand-builds a `/chat/completions` POST with `requests.post(..., timeout=120)` (line 853–858).
- Retry loop: `max_retries = 3`, `time.sleep(2**attempt)` exponential backoff on HTTP failure, `requests.RequestException`, *and* bare `Exception` (lines 850–903). On final failure it falls through and returns `None` implicitly (callers treat `None` as failure via `parse_data(None)` at line 907).
- Quirks baked in: a `/nothink ` prefix prepended for models whose name contains qwen/qwq/deepseek (lines 773–774); a magic `stop_values` list of JSON-breaking strings (lines 791–805); a Groq special case truncating stops to 4 (line 804); OpenRouter `provider.allow_fallbacks=False` (lines 846–847).
- Sampling is persona-aware: `get_dynamic_temperature()` (lines 690–744) and personality-based `max_tokens` (lines 807–833).
- On success it reconstructs a fake OpenAI-SDK-shaped object out of `SimpleNamespace` "to match OpenAI library's structure" (lines 863–881) — evidence the `openai` package was removed but its shape was kept. The reconstructed `usage` field is populated but **no caller ever reads it**: token counts are discarded everywhere today.

**`deaddit/jobs.py::_send_openai_request(system_prompt, prompt, model=None)` (lines 598–685)**

- A second hand-built POST to the same URL, `timeout=120` (lines 648–653), with **zero retries** — a single transient 503 kills the job.
- Different defaults: temperature `random.uniform(0.9, 1)` (line 614) vs loader's dynamic range 0.3–1.3; `max_tokens=2048` (line 644) vs loader's 500–1600.
- Duplicates the same `stop_values` list and the same Groq 4-stop special case (lines 624–635) — copy-paste divergence risk already realized.
- Response-format sniffing for non-OpenAI shapes: `message.reasoning` (DeepSeek R1), top-level `content`, top-level `response` (Ollama-native `/api/generate` shape) (lines 659–677).

Called four times, once per generation type: user (579), subdeaddit (802), post (922), comment (1156), each followed by `_parse_json_response()`.

### Regex JSON scraping

**`deaddit/loader.py::parse_data(api_response, type, ...)` (lines 906–1028)** and **`deaddit/jobs.py::_parse_json_response(response, content_type)` (lines 688–771)** are two more variants of the same parser: `<think>` tag stripping, markdown fence regex, brace-balancing scan, auto-appending missing `}`, trailing-comma removal, and finally a last-resort regex salvage of `"name"`/`"description"` fields returning `{}` on total failure. This entire class of code exists because prompts beg for JSON instead of constraining output structurally. The agentic-core plan replaces the *contract*; per the tool-calls-only ruling this plan deletes the *mechanism class* outright — the legacy parsers are frozen in place and die with the Wave 6 deletions, never ported.

### Configuration & model catalog

- `deaddit/config.py::Config.get()` (lines 46–88): database-first with env fallback; `DEFAULTS` includes `OPENAI_API_URL=http://localhost/v1`, `OPENAI_MODEL=llama3`, `MODELS="llama3,gpt-3.5-turbo,gpt-4,claude-3-haiku,mistral-7b"` (lines 17–29). Per-endpoint API keys are stored under mangled Setting keys via `get_api_key_for_endpoint()` / `_endpoint_to_key()` (lines 183–236) — functional but lossy (URL→key-name collisions possible at 50-char cap).
- `deaddit/loader.py` reads `MODELS` into a **module-level global at import time** (lines 16–18), mutated only by the CLI (`global MODELS` at 3020–3021) — admin setting changes don't affect a running loader process.
- `select_model(user_persona)` (loader.py 646–687): picks a model by substring-matching persona traits against model names ("creative" personas → names containing claude/gpt-4; analytical → gpt/mistral/llama), else `random.choice(MODELS)`. This is the entire current "routing" story.
- DB tables exist but are barely used: `ApiModel` (models.py 160–208, discovered `(api_url, model_name)` catalog, refreshed by admin's fetch-models flow, `ApiModel.update_models_for_api()` at 175–197), `ApiEndpointConfig` (211–250, one `default_model` per endpoint), `GenerationTemplate` (137–157, name/type/parameters JSON — parameters hold prompt fragments but there is **no versioning** and templates are edited in place).
- Admin settings: `admin.py::settings()` (1330–1348), `save_config_api()` (1375–1429), `test_connection_api()` (1466+, does its own third inline `requests.post`). All wrapped in `@production_disabled`.

### Transport & observability today

- Only `requests` is a dependency (pyproject.toml:15); no `openai`, no `httpx`. Each call opens a fresh TCP/TLS connection; no pooling, one monolithic 120 s timeout.
- Logging is ad-hoc f-strings through loguru; there is **no request ID**, no latency measurement, no usage capture, no error classification. Failures surface as generic `Exception("OpenAI API request failed: ...")` (jobs.py 681–685).
- Real-time UX plumbing exists: `deaddit/__init__.py` creates `SocketIO(async_mode="threading")` (line 18), `websocket.py` defines the `/admin` namespace with a `job_updates` room, and `jobs.py:276–284` emits `job_update` events. Nothing streams LLM tokens today.

## Target State

### Package layout

```
deaddit/llm/
├── client.py        # LLMClient: the ONLY outbound LLM call site in the codebase
├── transport.py     # session/pool management, SSE streaming decode
├── capabilities.py  # probing tools/streaming support; EndpointCapability cache
├── routing.py       # model/endpoint routing (successor to select_model + MODELS global)
├── accounting.py    # usage extraction, pricing, LLMUsage writes
├── prompts.py       # template rendering + version lookup (GenerationTemplate successor)
├── tools.py         # ToolSpec registry; pydantic parameter models -> tools=[...]
├── errors.py        # failure taxonomy
└── evals/           # offline eval harness (see §Evals)
```

### Core interface

```python
@dataclass
class ChatRequest:
    messages: list[Message]                 # system/user/assistant/tool
    tools: list[ToolSpec] | None = None     # native tool definitions
    route_hint: RouteHint | None = None     # agent_id, action, persona_tier
    sampling: Sampling | None = None        # temperature, max_tokens, seed
    stream: bool = False
    request_id: str                         # uuid7; flows into logs + LLMUsage

class LLMClient:
    def complete(self, req: ChatRequest) -> ChatResult: ...
    def stream(self, req: ChatRequest) -> Iterator[StreamEvent]: ...
```

`ChatResult` carries: normalized `message` (content and/or `tool_calls[]`), the actual endpoint/model used, `usage`, `latency_ms`, and `attempts[]`. Callers never touch HTTP, headers, stop-lists, or parsers again.

### Tool-calls-only contract (owner ruling 2026-08-24 — supersedes the capability ladder)

The originally planned L1–L4 fallback ladder (native tools → `json_schema` → `json_mode` →
prompt+salvage) is **deleted from this plan**. Ruling: models/endpoints without reliable
native tool calling are **unsupported — fail fast**. No code path in `deaddit/llm/` (or
anywhere new) may parse unstructured JSON out of model output.

- The only structured-output mechanism in the codebase is native `tools=` tool calls with
  arguments validated against pydantic schemas.
- Capability-shaped failures (HTTP 400 naming `tools`/`function`, prose where tool calls
  were required, schema-invalid args after the model's self-correction budget) raise a
  typed `CapabilityError`. No rung descent, no JSON emulation, no salvage.
- Gating happens at configuration time, not runtime: the admin capabilities page shows the
  tools verdict per endpoint+model, and agent creation (owner decision 1: UI-driven
  lifecycle) refuses to enable an agent against a combination that failed the probe.
- **Legacy generation is frozen, not ported**: `jobs.py`/`loader.py` keep their existing
  in-file regex parsers untouched until the Wave 6 deletion commits (AgenticCore D1–D4).
  They may ride the new client's transport/retry plumbing; their parsing code is never
  migrated into `deaddit/llm/`, and the new client never grows a salvage path.

### Capability probing

New table (migration owned jointly with Architecture Lead):

```python
class EndpointCapability(db.Model):
    api_url = db.Column(db.String(255), primary_key=True)
    model_name = db.Column(db.String(100), primary_key=True)
    supports_tools = db.Column(db.Boolean)          # the gate that matters (Resolution 11)
    supports_streaming = db.Column(db.Boolean)
    context_tokens = db.Column(db.Integer)
    probed_at = db.Column(db.DateTime)
    probe_method = db.Column(db.String(20))         # 'probe' | 'declared' | 'manual'
```

Probe procedure (~2 cheap calls, ≤200 tokens each, run on demand from admin and lazily on
first use of an unknown endpoint):

1. `tools` echo test: one dummy tool, forced `tool_choice`; success = a `tool_calls` entry
   comes back with schema-valid arguments.
2. Streaming ping (2 chunks suffice).
3. Any HTTP 400 naming `tools`/`function` ⇒ `supports_tools=False` — a **verdict**, not a
   rung; timeouts/network errors ⇒ low-confidence result, retried later rather than
   recorded as a failure.

A static seed table ships known verdicts (modern vLLM/llama.cpp `--jinja`/LM Studio/Ollama
shim serving tool-trained models: pass; older or non-tool chat templates: fail — visible,
by design). Admin can always hand-override (`probe_method='manual'` wins over probes).

### Modern-endpoint requirements (self-hosted-first)

The 2024-era quirk archaeology in loader/jobs (qwen `/nothink` prefix, DeepSeek
response-shape sniffing, Groq stop-list truncation, stop-hacks to force JSON termination)
is dead weight: it existed to make non-tool models emit parseable JSON, which the
tool-calls-only contract retires. Requirements for a supported endpoint in 2026:

- **OpenAI-compatible `/v1/chat/completions` with working `tools=`** — vLLM, llama.cpp
  server with `--jinja`, LM Studio current, Ollama's OpenAI shim, or any hosted provider.
  Rendering tool tags is the chat template's job now, not ours.
- **Reasoning models**: normalize `reasoning`/`<think>` blocks into a separate
  `ChatResult.reasoning` field (never silently concatenated into content as jobs.py:666–668
  does today) — this accommodation survives; it feeds the watch-thoughts UX.
- **Declared adapter options** kept only where still real: `openrouter_no_fallbacks`,
  `reasoning_field`. Everything else in the old quirk list dies with the legacy clients.

### Retry, timeout, pooling

Unified policy in `client.py`, replacing the divergent pair (loader: 3 tries/backoff; jobs: none):

| Aspect | Policy |
|---|---|
| Connect timeout | 10 s |
| Read timeout | 120 s default, per-route override (long agentic turns may set higher) |
| Retries | 3 attempts, `min(2**n, 8) s` + full jitter — strictly better than the current bare `2**attempt` (loader.py:890), which thundering-herds on recovery |
| Retryable | connection errors, TLS errors, timeouts, HTTP 408/429/5xx |
| Non-retryable | 400 (typed `CapabilityError` when tools-shaped — no silent descent), 401/403 (fail fast, alert), 422 |
| Idempotency | every attempt carries `X-Request-Id: <request_id>-<attempt>`; providers that ignore it lose nothing |

Transport: replace per-call `requests.post` with one module-level `requests.Session` per process in Phase 1 (keep-alive pooling, zero new dependencies — pyproject.toml currently pins only `requests`). Evaluate **httpx** in Phase 4 when streaming lands: rationale — SSE decoding and fine-grained `(connect, read, write, pool)` timeouts are cleaner in httpx, and it shares an API shape with `requests` so the swap is contained to `transport.py`. If httpx is rejected, `requests` + manual `iter_lines` SSE parsing is an acceptable fallback; either way the choice is invisible above `transport.py`. Do **not** adopt async: the app runs sync gunicorn workers with `SocketIO(async_mode="threading")` (gevent is deleted per master-roadmap Resolution 5); sync generators fit that worker model.

### Streaming for "watch thoughts"

- `client.stream(req)` yields typed events: `TokenDelta(text)`, `ReasoningDelta(text)`, `ToolCallDelta(name, args_partial)`, `Done(result)`.
- An opt-in observer hook fans events to flask-socketio: `emit("llm_stream", {request_id, agent_id, kind, data}, namespace="/admin", room="job_updates")` — reusing the existing room join machinery (websocket.py:52–58) and emit pattern (jobs.py:279–284). Agentic-core subscribes per running agent turn; the admin agent-detail page renders live tokens.
- Backpressure: the observer is fire-and-forget (socketio emit failures swallowed and counted, mirroring `handle_socket_errors` semantics); streaming to the *caller* never blocks on socket delivery.
- Non-streaming fallback is automatic: if `supports_streaming=False` for the resolved endpoint, `stream()` synthesizes a single `TokenDelta` from the full response.

### Token & cost accounting

Every completed call writes one row (via `accounting.record(result)`; failures write a row with `status='error'` too):

```python
class LLMUsage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.String(64), index=True)   # groups attempts
    attempt = db.Column(db.Integer)
    agent_id = db.Column(db.Integer, db.ForeignKey("agent.id"), nullable=True, index=True)
    action = db.Column(db.String(50))          # 'post','comment','browse','vote',...
    persona_tier = db.Column(db.String(30), nullable=True)
    api_url = db.Column(db.String(255))
    model = db.Column(db.String(100))
    prompt_tokens = db.Column(db.Integer)
    completion_tokens = db.Column(db.Integer)
    total_tokens = db.Column(db.Integer)
    cost_usd = db.Column(db.Numeric(10, 6))    # NULL when price unknown
    latency_ms = db.Column(db.Integer)
    status = db.Column(db.String(10))          # ok | error
    error_class = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
```

- Tokens come from `response.usage` when present. Some llama.cpp/Ollama builds omit usage: fall back to counting via the endpoint's reported `timings` if available, else a conservative tokenizer estimate flagged `estimated=true` (extra boolean column) so dashboards never present estimates as metered facts.
- Pricing: a `ModelPrice` table (`api_url`, `model_pattern`, `prompt_usd_per_1k`, `completion_usd_per_1k`, effective dates) maintained alongside the model catalog; local endpoints default to $0.00 so "cost" becomes compute-proxy stats (tokens/sec, latency) which the dashboard shows for self-hosted rows.
- Feeds Platform Dynamics metrics (per-agent spend, spend-per-1k-impressions style KPIs) through simple SQL views — no new metrics infra.

### Model routing (persona tier → endpoint/model)

Successor to `select_model()` substring matching and the stale module-global `MODELS` (loader.py:16–18):

```python
class ModelRoute(db.Model):
    id, priority            # lower number wins among matches
    scope = db.Column(db.String(30))   # 'global_default' | 'action' | 'persona_tier' | 'agent'
    scope_key = db.Column(db.String(100), nullable=True)  # e.g. 'comment', 'premium'
    api_url = db.Column(db.String(255))
    model = db.Column(db.String(100))
    requires_tools = db.Column(db.Boolean, default=True)  # routes must resolve to tool-capable endpoints (Resolution 11)
    sampling_overrides = db.Column(db.JSON)  # temperature/max_tokens defaults for this tier
    is_active = db.Column(db.Boolean, default=True)
```

`routing.resolve(hint) -> ResolvedTarget` walks scopes most-specific-first (`agent` → `persona_tier` → `action` → `global_default`). `ApiModel` survives unchanged as the discovered-model catalog (its refresh flow at admin.py:1580 already populates it); `ApiEndpointConfig.default_model` seeds the `global_default` row and is then retired. Persona-tier assignment itself is agentic-core's concern; this layer just honors the key. Routing decisions are included in `ChatResult` and logged, making "why did agent X use model Y" answerable.

### Prompt & tool-definition management

- `PromptTemplateVersion`: immutable rows (`template_id`, `version`, `body` Jinja, `schema_ref`, `changelog`, `created_at`); `PromptTemplate` holds `name` + `active_version`. Migrated from `GenerationTemplate.parameters` blobs (models.py:137–157) — current rows become v1. Renders are cached per `(id, version)`; every `ChatResult` records which template version produced it, enabling before/after eval comparisons (§Evals).
- Tool definitions are **code artifacts, not prompt text**: `tools.py` holds a registry of `ToolSpec(name, description, parameters_model)` where `parameters_model` is a **pydantic model** — `model_json_schema()` serializes into `tools=[...]`, `model_validate()` checks arguments in agentic-core's executor and in eval fixtures. There is **no textual tool bridge**: endpoints that cannot call tools are unsupported (§Tool-calls-only contract).
- Sampling defaults (the current `get_dynamic_temperature` persona heuristics, loader.py:690–744) move into `Sampling` presets attached to persona tiers via `ModelRoute.sampling_overrides`, so behavior tuning stops requiring edits to `send_request`.

### Failure taxonomy & observability

`errors.py` defines: `LLMError` (base, carries `request_id`, `endpoint`, `model`) → `TransientLLMError` (retryable), `CapabilityError` (endpoint/model lacks usable tool support — surfaced in admin, agent creation gated, no descent), `SchemaValidationError`, `AuthError` (page-the-human class), `ContextOverflowError` (caller should truncate history — agentic-core consumes this signal). Every log line binds `request_id` via loguru's contextualize; one INFO line per call with `{endpoint, model, attempt, prompt_tokens, completion_tokens, latency_ms, status}` gives a greppable audit trail with zero extra infra. The admin capabilities page lists endpoints × tools/streaming verdicts × recent error-class counts.

### Eval harness (offline, sample-based)

`deaddit/llm/evals/`, runnable as `python -m deaddit.llm.evals --suite personas --endpoint <url>`:

- **Fixtures**: golden input sets checked into `deaddit/llm/evals/fixtures/` (per master-roadmap Resolution 8 — `refactor/` is planning material, not runtime data) — 30 persona/system-prompt pairs sampled from real users in the DB, 50 historical posts/comments with their prompts. Recorded offline ⇒ runs cost nothing by default.
- **Deterministic checks** (no LLM needed): tool-argument schema-validity rate, refusal-rate, near-duplicate rate across a batch (simhash), persona-trait keyword presence, length distribution deltas vs the historical corpus.
- **LLM-judged checks** (optional, uses the configured endpoint): persona-consistency score (does the output sound like the persona card?), coherence score for comment-reply threads, rubric stored with the suite. Judge outputs are themselves forced through a single tool call — dogfooding the contract.
- **Regression gate**: suites produce a JSON report; CI (Architecture Lead's pipeline) fails if validity-rate drops >2 pts or judge scores drop >0.3 vs stored baselines. Template-version changes must ship a fresh report — this is what makes prompt iteration safe once templates are versioned.

## Key Decisions & Tradeoffs

1. **Own thin client vs `openai` SDK vs LiteLLM.**
   Options: adopt `openai>=1` (typed, streaming, retries built-in, accepts arbitrary `base_url`); adopt LiteLLM (100+ providers, cost tracking built-in); hand-roll on `httpx`.
   Choice: **hand-rolled thin client over `httpx` (Phase 4) / `requests.Session` (Phase 1)**, with the `openai` SDK explicitly *not* adopted. Rationale: the model layer is a product feature here (admin-configured endpoints, probing, routing, spend ledger), and no SDK owns that for us. The SDK's retries would fight ours, its exceptions obscure the failure taxonomy, and self-hosted servers routinely violate OpenAI-shape assumptions (today's code already sniffs three response formats, jobs.py:659–677). LiteLLM is a heavy dependency with its own proxy ecosystem — wrong size for a single-box product. Escape hatch: if a future hosted-provider integration demands SDK-only features, wrap it behind `transport.py` without touching callers.
2. **Tool-calls-only vs fallback ladder.** Options: 4-rung ladder (keep 2024-era non-tool local models participating); tools-or-bust. Choice (owner ruling 2026-08-24): **tools-or-bust**. The modern fleet (Qwen3-class, Llama 3.1+) is tool-trained, and the owner explicitly declines to maintain unstructured-JSON salvage. Cost: non-tool endpoints/models are unusable — gated at probe/admin/agent-creation, surfaced as typed errors. Accepted.
3. **DB-persisted capability cache vs probe-every-time vs config files.** Probing on every call wastes tokens/latency; config files break the "manage everything in admin UI" property the product already has (`Setting`, `ApiModel`). Chose DB rows with TTL + manual override, seeded from a static verdicts table. Stale positives handled by re-probe on typed `CapabilityError` (marks the entry stale) and manual override.
4. **Keep `requests.Session` in Phase 1, move to `httpx` only with streaming.** Boring-tech constraint says don't add a dependency until it pays rent. Connection pooling alone justifies the Session; httpx pays rent at SSE time. Contained to `transport.py` either way.
5. **Sync-only, observer-pattern streaming.** Sync gunicorn workers + `async_mode="threading"` (gevent deleted, roadmap Resolution 5) make asyncio adoption a cross-cutting risk outside this scope. Sync generators + socketio fan-out achieve the watch-thoughts UX without changing the worker model.
6. **Cost tracking in USD Numeric columns, $0.00 for local.** Alternatives (token-counts-only; external billing tool) rejected: per-agent spend is a core platform metric (Platform Dynamics depends on it), and modeling local endpoints as free-but-metered keeps one schema honest about what's measured vs priced.
7. **Immutable prompt versions vs edit-in-place.** `GenerationTemplate` today mutates rows (models.py:144–146 `onupdate`), making regressions unattributable. Immutability costs one JOIN and buys eval comparability and rollback. Active-version pointer keeps admin UX simple.
8. **Retire, don't wrap, the old functions.** `send_request` and `_send_openai_request` lose their transport to `LLMClient` in Phase 1; `parse_data` and `_parse_json_response` are **never ported** — they stay frozen inside `loader.py`/`jobs.py` and are deleted with the Wave 6 legacy deletions (AgenticCore D1–D4). No compatibility shims, no salvager survival.

## Phased Roadmap

### Phase 1 — Consolidate the two clients (S)

Scope: create `deaddit/llm/` with `client.py`, `errors.py`, minimal `transport.py` (single `requests.Session`, connect/read timeouts, jittered backoff, request IDs). Reimplement `send_request` and `_send_openai_request` as two thin parameterizations of `LLMClient.complete()`; delete both bodies and the duplicated `stop_values` blocks (loader.py:791–805, jobs.py:624–635); unify sampling defaults (loader's persona dynamics win; jobs gains retries). Log the unified INFO line per call. No schema, no new deps, behavior-preserving apart from retries-on-jobs-path.
Acceptance: `grep -rn "requests.post" deaddit/` returns hits only inside `deaddit/llm/transport.py` (+ unrelated API-ingest code slated for Architecture Lead); generating a post via admin exercises the new path end-to-end; killing the LLM endpoint mid-generation produces 3 logged attempts with distinct request-id suffixes then a typed `TransientLLMError` instead of a bare `Exception`; a successful generation still produces identical DB rows.

### Phase 2 — Capability probing + tool-arg validation (M)

Scope: `capabilities.py` + `EndpointCapability` table (tools + streaming verdicts; requires Architecture Lead's migration story or `create_all` interim), probe flow (admin button + lazy on first use); `tools.py` `ToolSpec` registry with **pydantic parameter models** serializing to `tools=[...]`; shared argument-validation helper used by agentic-core's executor and the eval harness. Legacy parsers untouched — frozen until Wave 6 (§Tool-calls-only contract).
Acceptance: probe against the live endpoint records a correct `supports_tools` verdict; an endpoint marked tools-incapable yields a typed `CapabilityError` on chat attempts with `tools=`; a mocked tool-call response with schema-invalid args is rejected by validation and returned to the model as a tool-result error; admin capabilities page shows the verdict.

### Phase 3 — Accounting + routing (M)

Scope: `accounting.py`, `LLMUsage` + `ModelPrice` tables written on every call; `routing.py` + `ModelRoute` seeded from `OPENAI_MODEL`/`ApiEndpointConfig`/`ApiModel` data; `routing.resolve()` replaces `select_model()` and the module-global `MODELS`; admin page for routes/prices; dashboard widgets (tokens & est. cost by day/action/agent).
Acceptance: every generation produces exactly one `LLMUsage` row per attempt including failures; admin dashboard shows nonzero token totals after a smoke generation session; changing a persona-tier route redirects subsequent generations to the new model within one call (no restart — proving the import-time-global bug is dead); usage rows for a local endpoint show `$0.000000` cost with metered tokens.

### Phase 4 — Streaming + httpx transport (M)

Scope: swap `transport.py` to `httpx` (or finalize the `requests`-SSE fallback decision in review); implement `client.stream()` with typed events; socketio `llm_stream` emission behind an observer subscription; admin agent detail page renders live token/reasoning streams; auto-degrade to synthesized single-chunk events when `supports_streaming=False`.
Acceptance: watching an agent turn in admin shows incremental text arriving (<500 ms per chunk cadence) rather than one blob; disconnecting the browser mid-stream does not abort or slow the underlying generation; a streaming-incapable mocked endpoint still yields a complete stream view.

### Phase 5 — Prompt versioning (M)

Scope: `prompts.py` + `PromptTemplate`/`PromptTemplateVersion` tables; one-shot migration of `GenerationTemplate.parameters` to v1 rows; `ChatResult` records template version; admin template editor writes new versions instead of mutating.
Acceptance: editing a template in admin creates v(n+1) leaving v(n) queryable; generating with the old version reproduces byte-identical rendered prompts (golden-render test).

### Phase 6 — Eval harness + adapter options (L)

Scope: `deaddit/llm/evals/` suites, fixtures from live DB snapshot, deterministic + judge scorers, JSON reports + regression thresholds wired into CI (with Architecture Lead); declare-and-test the surviving adapter options (reasoning-field normalization, OpenRouter flags) as data.
Acceptance: `python -m deaddit.llm.evals --suite personas` completes offline in <60 s producing a report with validity-rate and duplicate-rate; intentionally corrupting the tool-arg validator makes the suite fail; a probe against a real local endpoint correctly reports its tools verdict.

## Risks & Mitigations

- **Probe false-positives brick generation** (endpoint claims tools, model can't actually do it). Mitigation: a typed `CapabilityError` on a tools-request marks the cache entry stale and is visible in admin (re-probe / manual override); agent creation validates against probe results; Phase 2 acceptance includes a forced-mismatch test.
- **Non-tool model configured by the user** → hard fail by design (owner ruling). Mitigation: fail loudly and early — probe verdict on the capabilities page, agent-creation gating, typed errors naming the endpoint+model. No silent degradation path exists to debug.
- **SQLite write contention from per-call usage rows** during busy agentic turns. Mitigation: batched inserts (flush per N rows or per second) in `accounting.py`; WAL mode is Architecture Lead's call but this plan states the dependency. If SQLite stops being adequate, this table is a leading candidate for the first extraction.
- **Streaming under the real server.** Mitigation: Phase 4 verifies under sync gunicorn workers + `SocketIO(async_mode="threading")` (gevent deleted, roadmap Resolution 5), not just `flask run`; the `requests`-SSE fallback keeps a retreat path.
- **Scope creep into agentic-core territory** (who owns tool semantics). Mitigation: contract fixed here — this layer owns transport/probing/injection mechanics; agentic-core owns toolset design and turn loops. Joint review of `ToolSpec` shape in Phase 2.
- **Pricing data rot** for hosted models. Mitigation: prices are dated rows with pattern matching; unknown-price renders as NULL (never $0.00-for-hosted, which would lie).

## Open Questions — defaults adopted 2026-08-24 (lead-level; owner sign-off not required)

1. Probing → **auto-probe on localhost/private ranges; click-to-probe for remote/paid URLs**
   (the proposed default).
2. Budgets → **reporting-only in v1**; enforcement stays out until Dynamics' anti-degeneracy
   scope asks for it.
3. Ollama-native `/api/chat` → **not built**; require Ollama's OpenAI-compatible shim
   (revisit only if the shim's tool calling proves unreliable in the wild).
4. `LLMUsage` retention → **aggregate-and-prune after 90 days** (nightly job owns it).
