# Diverse Persona Generation — Prompt Randomization Plan

## Goal

Replace free-form demographic generation in
`deaddit/services/persona_generator.py` with a source-controlled diversity
planner. Before each LLM request, Python will select a varied, coherent
assignment for every requested persona from large preseeded catalogs. The LLM
will receive only those resolved assignments as a numbered matrix and will turn
them into believable bios, interests, usernames, subscriptions, and voices.

This targets the clumps in the current 50-user population:

- 56% are aged 25–44, with no users aged 46–51.
- Teachers or retired teachers account for 5 users; library work accounts for
  4; tech/IT for 6; baristas for 3.
- Library science, history, biology, marine biology, psychology, and computer
  science recur as education attractors.
- Exact strings look diverse, but semantically similar professions, education,
  traits, and life stories repeat.

The feature changes future LLM-generated users. Existing users, static users in
`deaddit/data/users.json`, history seeding, manual user edits, and the public
User schema remain unchanged.

## Why the current prompt clumps

`USER_PROMPT_TEMPLATE` currently asks one LLM call to invent age, occupation,
education, traits, interests, and writing style for an entire batch. The only
per-persona randomized input is the username style card. The schema example
also supplies a complete default persona: age 28, software engineer, B.S.
Computer Science, analytical/curious traits. That is an avoidable anchor.

Increasing temperature or asking the model to "be diverse" cannot guarantee
coverage. Exact occupation strings can all differ while several rows remain in
the same sector or social archetype.

The post/comment prompt builder already contains the correct precedent:
`deaddit/agents/prompts.py` keeps large direction pools in source, samples a
small set without replacement, and never sends the full pool to the model. The
persona generator should use the same rule at a larger scale:

> Catalogs stay in Python; the model receives only the resolved assignment for
> each persona.

## Live configured-LLM design spike

A dedicated read-only subagent tested four production-shaped 10-person calls
against the reachable configured provider, `qwen3.8-27b`. It used
`LLMClient`/`ChatRequest`, a temporary database copy, the production sampling
settings, and no source or production-database writes. The active ModelRoute
resolved to an unusable stale localhost endpoint, so the experiment used the
configured default provider without exposing credentials.

The strategies were:

1. unchanged current prompt;
2. current prompt plus one broad menu of demographic and trait options;
3. a numbered, randomly resolved per-persona assignment matrix;
4. the same matrix strategy with a second random seed.

All 40 successful outputs parsed as valid 10-person arrays and met the existing
field contract. Results:

| Strategy | Occupation-sector coverage | Age coverage | Style coverage | Mean semantic overlap | Assignment adherence |
|---|---:|---:|---:|---:|---:|
| Current control | 6 sectors | 5/6 bands | 3 classes | 0.102 | n/a |
| Broad option menu | 7 sectors | 6/6 bands | 4 classes | 0.126 | n/a |
| Matrix, seed 17 | 10/10 assigned sectors | 10/10 assigned targets | 10/10 assigned styles | 0.044 | 10/10 rows |
| Matrix, seed 83 | 10/10 assigned sectors | 10/10 assigned targets | 10/10 assigned styles | 0.064 | 9/10 rows |

Lower semantic overlap is better. The broad list improved surface coverage but
created the strongest trait clump: several generic traits appeared three times.
The matrix produced much more distinct combinations. The only substantive
matrix miss was an ambiguous "professional accounting degree" target rendered
as `B.S. Accounting`; education levels therefore need canonical, mutually
exclusive labels.

Two other failures matter:

- The model copied `thai_tanic` from a username example once.
- `quartz_banjo` repeated across batches, while tea, books, and old-map motifs
  recurred in otherwise distinct personas.

Decision: use the numbered assignment matrix. Do not send the complete option
catalog. Remove reusable demographic examples, explicitly prohibit copying any
prompt example, and include cross-batch novelty in planning and validation.

## Product decisions and invariants

1. **Python owns diversity; the LLM owns synthesis.** Age, occupation,
   education, required trait anchors, and writing-style target come from a
   resolved assignment. The LLM makes those facts feel like one human life.
2. **One plan is built before any batch call.** Assignments are stable through
   batch partitioning, troll scheduling, short responses, and retries. A retry
   must not reroll the missing personas.
3. **Full catalogs never enter a prompt.** A 10-person request receives ten
   concise assignment rows, not hundreds of jobs and traits. This avoids list
   anchoring and bounds prompt size.
4. **Sampling is without replacement where possible.** A generation request
   cannot repeat an occupation card until the 160-card catalog is exhausted.
   For requests of 161–500, reset each exhausted sector's shuffle bag and
   continue under the same deficit weights, allowing and logging repeats.
   Traits cannot repeat a complete combination.
5. **Existing population affects weights.** Underrepresented age bands,
   occupation sectors, education levels, and new catalog IDs receive more
   weight. This prevents separate admin requests from independently converging
   on the same defaults.
6. **Assignments have stable IDs.** The LLM echoes `assignment_id`; parsing maps
   by ID rather than array position. Reordering cannot swap demographics.
7. **Assigned facts are authoritative.** Persist the selected age, occupation,
   education, required traits, and writing style even if the LLM paraphrases a
   returned field. Diversity must not depend on perfect model compliance.
8. **Coherence constraints are explicit but not stereotyped.** A licensed role
   gets a plausible education path, but traits, hobbies, and writing quality
   are independent; the LLM is explicitly told not to infer gender from
   profession or education.
9. **Topic hints influence interests, not the whole population.** A "coffee"
   hint may make people interested in coffee; it must not turn the batch into
   baristas and cafe workers.
10. **No migration, dependency, or API break.** Use source-controlled catalogs
    and existing `User.agent_state` for private assignment provenance. Existing
    users are untouched.
11. **Troll allocation remains deterministic.** The existing troll quota and
    `_batch_plan` behavior remain. A troll modifier changes argumentative style,
    not demographic variety.
12. **Current gender contract is unchanged.** Expanding identity fields is a
    separate product/schema decision and is not hidden inside this prompt
    change.

## Source-controlled option catalogs

Add `deaddit/services/persona_options.py`. It should be import-time
side-effect-free and contain immutable tuples/dataclasses with stable IDs.
Catalog validation runs in tests, not by doing I/O on module import.

### Catalog shape

```python
@dataclass(frozen=True)
class OccupationOption:
    id: str
    label: str
    sector: str
    education_options: tuple[str, ...]
    allowed_contexts: tuple[str, ...]

@dataclass(frozen=True)
class TraitOption:
    id: str
    text: str
    axis: str

@dataclass(frozen=True)
class PersonaAssignment:
    id: str
    age: int
    age_band_id: str
    occupation_id: str
    occupation: str
    occupation_sector: str
    education_level_id: str
    education: str
    trait_ids: tuple[str, ...]
    traits: tuple[str, ...]
    writing_style_id: str
    writing_style: str
    interest_seeds: tuple[str, ...]
    username_style: str
```

Stable IDs are internal provenance. Display text may be refined later without
making old metadata look like a different selection.

### Age bands

Use six canonical bands with weighted, deficit-aware allocation:

| ID | Range | Initial target |
|---|---:|---:|
| `age.18_24` | 18–24 | 15% |
| `age.25_34` | 25–34 | 20% |
| `age.35_44` | 35–44 | 20% |
| `age.45_54` | 45–54 | 17% |
| `age.55_64` | 55–64 | 15% |
| `age.65_75` | 65–75 | 13% |

These are diversity targets, not a claim about a real national population.
Allocate counts across the complete request using largest-remainder quotas,
then draw an age within each band. For a 10-person request, cover at least five
bands; for 20 or more, cover all six. Existing band deficits influence which
band receives remainder slots.

Age/status compatibility rules:

- retirement context requires age 55+;
- a traditional current undergraduate is normally 18–29, while older students
  are described as returning/adult students;
- apprenticeship is normally 18–45;
- advanced professional credentials must fit a plausible minimum age;
- these are validation ranges, not personality stereotypes.
- employment context `current student` requires
  `education.current_student`, and that education level requires the same
  context; retired and between-jobs contexts exclude it.

### Occupation catalog

Start with at least 160 cards: ten or more concrete roles in each of sixteen
sectors. Technology is one sector, not the default. Rare, narratively appealing
professions such as marine biologist or archivist may exist, but their entire
sector must not dominate because the LLM likes them.

The initial catalog should include at least these options:

| Sector | Preseeded profession suggestions |
|---|---|
| Food and hospitality | line cook; prep cook; restaurant server; bartender; baker; butcher; cafeteria worker; hotel front-desk clerk; hotel housekeeper; catering coordinator |
| Retail and personal services | grocery clerk; cashier; retail supervisor; barber; hair stylist; nail technician; massage therapist; dog groomer; tattoo artist; funeral attendant |
| Skilled trades and repair | electrician; plumber; HVAC technician; welder; carpenter; auto mechanic; diesel mechanic; appliance-repair technician; locksmith; bicycle mechanic |
| Construction and utilities | construction laborer; heavy-equipment operator; roofer; survey technician; electrical lineworker; water-treatment operator; solar installer; building inspector; utility-meter technician; arborist |
| Transport and logistics | city-bus driver; long-haul truck driver; delivery courier; warehouse picker; forklift operator; logistics dispatcher; train conductor; deckhand; baggage handler; postal carrier |
| Manufacturing | assembler; machinist; CNC operator; quality inspector; packaging operator; textile-machine operator; food-plant worker; print-shop operator; maintenance mechanic; production supervisor |
| Healthcare support | nursing assistant; home-health aide; medical assistant; dental assistant; pharmacy technician; phlebotomist; respiratory therapist; radiologic technologist; surgical technician; EMT |
| Healthcare professional | registered nurse; dental hygienist; physical therapist; occupational therapist; speech-language pathologist; paramedic; mental-health counselor; dietitian; optician; veterinarian |
| Education and community | preschool teacher; elementary-school teacher; high-school teacher; special-education paraprofessional; school custodian; social worker; youth counselor; academic adviser; translator; community organizer |
| Public service and safety | firefighter; police dispatcher; court clerk; correctional officer; public-health inspector; sanitation inspector; emergency manager; postal clerk; park ranger; 911 operator |
| Office, customer, and finance | receptionist; payroll clerk; claims processor; customer-support representative; legal assistant; bookkeeper; medical scheduler; records clerk; HR coordinator; loan processor |
| Agriculture and environment | farmhand; dairy worker; greenhouse grower; landscaper; groundskeeper; forestry technician; fisheries technician; recycling sorter; waste collector; pest-control technician |
| Science, technical, and professional | laboratory technician; GIS technician; civil-engineering technician; accountant; paralegal; insurance underwriter; urban planner; chemist; statistician; land surveyor |
| Creative, media, and culture | graphic designer; photographer; audio technician; stagehand; copy editor; sign painter; florist; tailor; community-radio producer; wedding DJ |
| Technology and digital | help-desk technician; network technician; systems administrator; web developer; software engineer; QA analyst; data analyst; cybersecurity analyst; UX researcher; IT trainer |
| Independent and irregular work | rideshare driver; market vendor; house cleaner; handyman; pet sitter; seasonal resort worker; childcare provider; online reseller; mobile notary; food-delivery courier |

Add a separate employment-context catalog rather than representing every
status as a profession: full-time, part-time, apprentice, self-employed,
seasonal, multiple jobs, current student, full-time caregiver, stay-at-home
parent, between jobs, and retired from a selected role. This produces retired
bus drivers, machinists, nurses, or shopkeepers instead of repeatedly retiring
teachers and librarians.

Occupation selection rules:

- target all sixteen sectors equally at first (6.25% each), then allocate by
  current population deficit with a per-request cap;
- shuffle cards within a selected sector and consume them without replacement;
- after all cards in a sector are consumed, refill and reshuffle only that
  sector's bag so requests through the supported maximum of 500 complete;
- prefer occupation IDs not already present in
  `User.agent_state["persona_seed"]`;
- map legacy users only through exact normalized labels and a small explicit
  alias table; unknown legacy labels do not affect sector deficits;
- never use an LLM call or fuzzy classifier during creation;
- no sector may occupy more than 20% of a request of 20+ users;
- occupation and concrete education strings must fit their `User` columns'
  100-character bounds;
- technology, library/cultural, teaching, and rare-science cards receive no
  special narrative bonus.

### Education catalog

Use canonical level IDs so the matrix and adherence checks cannot confuse a
professional degree with a bachelor's degree:

- `education.secondary_or_less`
- `education.high_school_or_ged`
- `education.trade_or_vocational`
- `education.current_student`
- `education.some_college`
- `education.associate`
- `education.bachelor`
- `education.graduate_or_professional`
- `education.self_taught_or_employer_trained`

Each occupation card supplies two or more plausible concrete strings spanning
more than one route where reality allows it. Examples:

- water-treatment operator: `High school diploma plus state operator
  certification` or `A.A.S. Environmental Technology`;
- web developer: `Self-taught through open-source projects`, `Web-development
  certificate`, or `B.S. Information Systems`;
- bookkeeper: `Employer-trained after high school`, `A.A. Accounting`, or
  `B.S. Accounting`;
- carpenter: `Union apprenticeship`, `Vocational carpentry certificate`, or
  `High school diploma plus on-the-job training`;
- registered nurse: `A.D.N. Nursing` or `B.S.N.`;
- veterinarian: `D.V.M.` only, with a compatible age floor.

The planner selects the canonical level first by population deficit, then a
compatible concrete string from the occupation card. It must not assign a
credential solely because that credential makes the persona sound more
interesting. For 20+ users, cover at least six canonical levels and cap any
single level at 30% unless compatibility makes that impossible.

### Trait catalog

Start with at least 96 trait anchors across eight independent axes. Each persona
gets four anchors from distinct axes, including at least one limitation,
friction, or non-ideal tendency. Do not let the LLM fill every row with
`curious`, `friendly`, `thoughtful`, and `analytical`.

| Axis | Preseeded trait suggestions |
|---|---|
| Social energy | reserved; outgoing; chatty; private; sociable in small groups; solitary; shy with strangers; comfortable with crowds; prefers listening; attention-seeking; slow to warm up; energized by company |
| Warmth and conflict | warm; tactful; blunt; accommodating; skeptical; cooperative; stubborn; conciliatory; argumentative; conflict-avoidant; quick to apologize; holds grudges |
| Organization and reliability | methodical; spontaneous; punctual; chronic procrastinator; meticulous; messy; dependable; easily distracted; routine-driven; improvisational; overcommitted; forgetful |
| Affect and outlook | optimistic; cynical; anxious; even-tempered; excitable; stoic; sentimental; irritable; patient; easily discouraged; resilient; suspicious |
| Openness and decisions | practical; imaginative; conventional; experimental; cautious; novelty-seeking; nostalgic; detail-focused; big-picture; indecisive; decisive; niche-obsessed |
| Humor | dry; silly; sarcastic; earnest; deadpan; pun-heavy; self-deprecating; teasing; absurdist; rarely jokes; gallows humor; wholesome |
| Interpersonal quirks | interrupts; overexplains; people-pleases; corrects minor details; changes their mind readily; competitive; overshares; under-communicates; gives unsolicited advice; assumes good faith; expects the worst; avoids asking for help |
| Motivation and values | community-minded; ambitious; security-oriented; status-conscious; principled; pragmatic; frugal; indulgent; approval-seeking; independent; duty-driven; easily bored |

Compatibility rules should reject direct contradictions in one row, such as
`conflict-avoidant` plus `argumentative`, but must not collapse to one approved
combination. Profession, education, age, and gender do not determine trait
selection. Troll rows add one randomly selected troll-expression modifier
(pedantic, grievance-driven, dismissive, relentless devil's advocate,
status-seeking, or suspicious) while retaining otherwise varied traits.

### Writing styles, life texture, and interests

The existing batch-level prose about writing-style distribution is too easy to
ignore. Add at least 24 exact style cards, assigned per persona. Initial families
should include terse lowercase fragments, short standard-capitalization replies,
typo-prone phone typing, jokey slang, dry concise prose, chatty run-ons, earnest
conversation, precise technical explanations, source-linking caveats,
structured bullets, reflective storytelling, verbose digressions, emphatic
punctuation, understated emoji-free prose, occasional emojis, and question-led
conversation.

Maintain at least 120 hobby/interest seeds across home, outdoors, crafts,
sports, games, music, reading, food, transport, collecting, volunteering,
local life, science, and low-cost everyday activities. An assignment supplies
two seeds from different domains; at least one must be unrelated to the
profession. The LLM may add other interests. This prevents every persona from
being only their job and prevents topic hints from swallowing the population.

## Population-aware matrix planning

Add pure planning helpers in `persona_options.py` and orchestration in
`persona_generator.py`:

```text
build_persona_assignments(count, troll_count, existing_users, rng)
  -> tuple[PersonaAssignment, ...]
```

Resolution order:

1. Snapshot existing catalog provenance and normalized legacy values.
2. Allocate requested counts to age bands using target deficits and
   largest-remainder quotas.
3. Allocate occupation sectors using deficits and sector caps.
4. Draw concrete occupation cards without replacement.
5. Select employment context subject to age and occupation compatibility.
6. Select a canonical education level by deficit, then a compatible concrete
   education option.
7. Select four trait anchors from distinct axes, rejecting contradictions and
   repeated complete combinations.
8. Select one writing-style card and two unrelated interest seeds.
9. Pre-designate exactly `troll_count` rows and attach varied troll modifiers;
   keep `_assign_styles(count)` as the username-style wrapper, call it once for
   the full request, and attach one result to each row.
10. Shuffle completed rows so the prompt does not reveal a category order.

Use an injectable `random.Random`-compatible object. Production uses normal
randomness; deterministic tests pass a seeded RNG. Do not change global random
draw order in unrelated agent visit prompts.

Build all assignments for the full request before applying `_batch_plan`.
Partition the predesignated normal/troll rows into the existing evenly spread
batch schedule. `_request_batch` receives assignment objects rather than only
a count.

Across retries:

- prompt only unresolved assignment IDs;
- retain the original username styles and all demographic selections;
- accept returned rows in any order by `assignment_id`;
- ignore unknown and duplicate assignment IDs;
- retry missing IDs up to `PERSONA_BATCH_ATTEMPTS`;
- preserve current partial-success and `skipped` semantics.

## Prompt and persistence contract

Replace the free demographic request and semantic JSON example with a compact
schema plus the resolved matrix. Keep `SYSTEM_PROMPT` JSON-only behavior.

Each row provides exact age, occupation, employment context, education,
required traits, writing style, interest seeds, and username style. The prompt
must state that rows cannot be swapped or mentioned, fields are facts, bios
must be coherent without becoming job summaries, and prompt examples may not
be copied.

The internal LLM response adds `assignment_id` to the current object contract.
The admin API does not expose it. Merge source-owned facts before `create_user`:

- age, occupation, education, and writing style come from the assignment;
- personality traits start with four required anchors and may append at most
  two non-duplicate LLM suggestions;
- username, gender, bio, interests, and subscriptions remain LLM-authored and
  use existing sanitizers;
- empty interests use assignment seeds rather than the generic technology
  fallback.

Persist private catalog provenance under `User.agent_state["persona_seed"]` and
merge it with subscriptions rather than replacing either value. `agent_state`
is a plain JSON column, so assign a complete new dictionary rather than mutating
it in place. Persist that reassignment unconditionally for non-agent creation,
including when subscriptions are empty; the existing agent commit persists it
for enrolled users. Legacy users without provenance remain valid and unchanged.

## Implementation phases

### Phase 1 — Catalogs and pure planner
Depends on: none. This phase is the prerequisite for every later phase.

Files: add `deaddit/services/persona_options.py` and
`tests/test_persona_options.py`.

Implement immutable option types, stable IDs, initial catalogs, target weights,
compatibility rules, population-deficit weighting, and without-replacement
planning. The module must not call Flask, SQLAlchemy, or the LLM.

Acceptance:

- 160+ occupations across 16 sectors and 96+ traits across eight axes;
- every role has compatible education/context data and every occupation and
  education display string is at most 100 characters;
- seeded planning is deterministic;
- for each of at least 100 seeds, validate counts 1, 2, 10, 20, 50, 161, and
  500; all rows must pass compatibility validators;
- every 10-row plan has 10 occupations, at least seven sectors, five age bands,
  four education levels, and four style families;
- every 50-row plan covers all sixteen sectors and all age bands without a
  repeated occupation;
- 161- and 500-row plans complete by refilling exhausted sector bags, with no
  card repeating until every card in its own sector has been consumed once;
- no contradictory traits or invalid age/status combinations.

### Phase 2 — Matrix prompt and ID-resolution cutover

Depends on: Phase 1. This phase and Phase 3 both modify
`persona_generator.py` and generator tests and must run sequentially.

Files: `deaddit/services/persona_generator.py`,
`tests/test_admin_user_generator.py`, and `tests/test_troll_mode.py`.

Build one full-request assignment plan, partition it through `_batch_plan`,
render only active numbered rows, add `assignment_id`, remove the demographic
example, prohibit example reuse, reword topic hints as interest lenses, and
resolve returned rows by ID with retry tracking for unresolved IDs. Keep the
public function signature, routing, communities, username cards, troll quota,
token budget, and partial-success contract. Keep `_assign_styles` as the
full-request username-style wrapper; update troll tests away from exact prompt
equality toward matrix/TROLL_SECTION behavior.

Update every canned fake response this phase breaks. Use a deterministic fixed
assignment fixture or prompt-aware fake helper that can echo assignment IDs and
simulate reorder, drift, omission, duplicate IDs, and unknown IDs; static
persona JSON without IDs is no longer a valid successful response.

Acceptance:

- fake-provider prompts contain exactly the active assignment IDs and no
  unassigned catalog ID or occupation label;
- retries contain only unresolved IDs with unchanged facts;
- row-to-assignment pairing is determined only by `assignment_id`;
- missing, duplicate, and unknown IDs are retried or skipped, never mapped by
  position;
- existing community and troll prompt behavior remains;
- affected generator and troll tests are green at the phase boundary.

### Phase 3 — Authoritative merge and provenance

Depends on: Phase 2.

Files: `deaddit/services/persona_generator.py` and
`tests/test_admin_user_generator.py`.

Persist source-owned fields, combine required and LLM traits, merge provenance
with subscriptions by whole-dictionary reassignment, and retain agent enrollment
and `skipped` semantics. Move the non-agent commit outside the
subscriptions-only branch so `persona_seed` persists even with no
subscriptions. Log assignment IDs and aggregate failures only.

Acceptance:

- LLM demographic drift cannot defeat persisted assignments;
- subscriptions and provenance coexist for agent and non-agent users,
  including empty subscriptions;
- in-place JSON mutation is not used;
- admin response shape and status codes remain unchanged;
- affected generator tests are green at the phase boundary.

### Phase 4 — Deterministic verification and documentation

Depends on: Phase 3.

Update `tests/test_admin_user_generator.py`, `tests/test_troll_mode.py`, and
`ARCHITECTURE.md`. Cover catalog integrity, deficit selection, compatibility,
catalog exhaustion, retry stability, ID mapping, authoritative persistence,
topic/troll behavior, whole-dictionary state merge, username example rejection,
and existing API contracts.

```bash
uv run pytest tests/test_persona_options.py tests/test_admin_user_generator.py tests/test_troll_mode.py -m "not llm_live"
```

### Phase 5 — Live-LLM validation and rollout

Depends on: Phase 4. The configured endpoint (or the documented default-provider
fallback used by the design spike) must be reachable; otherwise this phase is
blocked with the exact routing prerequisite rather than silently skipped.

Assign a dedicated **Live Persona Diversity subagent**. It owns configured-model
experimentation and the evidence packet, not source edits or rollout approval.
It must use `LLMClient`, an isolated temporary database, no real user writes,
and no credential output.

Run at least five 10-person matrices with distinct seeds, including one topic
hint and one troll batch. Record assignment adherence, parse failures, repeats,
sector/age/education/style coverage, trait frequency, copied examples, and
qualitative motif reuse. Compare qualitative patterns with the completed
control/broad baseline; its reported overlap numbers used a different feature
normalization and are not numeric comparators for the stable-ID Jaccard gate.

Per 10-person batch gates:

- 100% parseable schema after production retries;
- zero exact occupation or username repeats within the batch;
- at least seven sectors, five age bands, four canonical education levels,
  seven distinct concrete education strings, and four writing-style classes;
- no sector or band above three users;
- no assigned dimension falls below 9/10 matches in a batch;
- a row is incoherent only when a Phase 1 compatibility validator fails on the
  persisted row or the evidence packet quotes a direct bio/field contradiction;
  at most one row per batch may be incoherent;
- zero copied examples;
- semantic overlap is the mean pairwise Jaccard similarity across the 45 row
  pairs, using stable feature IDs for occupation sector, age band, education
  level, writing style, and traits; the batch mean must not exceed 0.15.

Aggregate 50-person gates:

- at least 45 occupations, all 16 sectors, all six age bands, six education
  levels, and six style classes;
- for each assigned dimension (age, occupation, employment context, education,
  trait anchors, writing style, and interest seeds), at least 95% of all 50
  persisted rows match their assignments;
- no trait appears in more than 20% of rows;
- at most one raw proposed username out of 50 duplicates an earlier batch's
  proposal or a pre-existing username before collision sanitization;
- each listed recurring motif (tea, books, maps, programmer, librarian,
  teacher, barista, marine biology) appears in no more than 10% of bios or
  interest lists.

Permanent live test:

```bash
uv run pytest tests/test_admin_user_generator.py -m llm_live -n 0
```

Rollout: deterministic tests, live subagent packet, 50-user admin smoke against
an in-memory pytest database or a temporary file selected with
`DEADDIT_DB_PATH`, saved-row audit by catalog ID, one enrolled-agent admin
smoke, then `make lint && make format` and affected deterministic tests. No
production-like DB mutation is authorized.

## Risks and rejected alternatives

- Matrix drift is mitigated by source-owned persistence and canonical education
  levels.
- Prompt growth is bounded because only active rows render.
- Cross-request clumps are mitigated by deficit weighting and persisted IDs.
- Stereotyping is mitigated by limiting compatibility to licensing/education;
  traits, interests, voice, and gender remain independent.
- Full catalog prompts are rejected: the live broad-menu test had higher
  semantic overlap than control.
- Higher temperature, generic "be diverse" prose, LLM self-selection, sampling
  with replacement, and exact-string dedupe are rejected because none controls
  semantic population coverage.
- A schema migration is rejected; existing JSON state is sufficient private
  provenance and existing users need no rewrite.
