# JARVIS — Frozen Decisions (Phase 1)

Frozen on 2026-09-30 by Lucas's authorization to begin Phase 1 implementation.
Authority order: (1) MASTER_PROMPT.md (87 sections, constitution — wins on conflict),
(2) Phase 1 Detailed Design PDF, (3) implementation brief from parent agent.

## Conflict scan (design PDF vs master prompt)

No material conflicts found. Checked: stack, ports/adapters, pure domain,
17-step sequence, 3 NEXUS GETs, timeouts/retry/circuit-breaker values,
deny-by-default policy with single capability `nexus.status.read`,
append-only audit, FakeLLM-before-OpenAI, bind 127.0.0.1, migrations 0001/0002,
7-state task machine, no-NEXUS-touch boundary.

One deliberate deviation from the design PDF: `.env.example` suggested
`JARVIS_PORT=8100`, but port 8100 collides with the nexus-deploy proxy on this
machine. Default port is **8123**. The master prompt only mandates loopback
bind, so this does not conflict with it.

## Frozen decisions

- D1. JARVIS = new independent repo at `~/workspace/jarvis/`. NEXUS untouched:
  no reads of its SQLite, no imports, no modifications, no process control.
- D2. Local-only, single-user. Bind 127.0.0.1, default port 8123. No cloud,
  tunnel, remote access, or public exposure.
- D3. `git init` only. NO commit, NO push, no history rewrite — separate
  authorization from Lucas required.
- D4. Dependencies: exactly the Phase 1 stack (fastapi, uvicorn, pydantic v2,
  pydantic-settings, httpx, sqlalchemy 2.x, aiosqlite, alembic, pytest,
  pytest-asyncio, ruff, mypy). Versions pinned in requirements.txt/pyproject
  from the lock resolved during implementation. No other runtime dependency.
  The OpenAI provider uses the REST API over httpx — no `openai` SDK installed.
- D5. Policy: deny-by-default. Exactly one capability: `nexus.status.read`
  (GET, `/api/v1/` prefix, loopback-only destination, redirects disabled,
  max 256 KiB). No wildcards. Policy is deterministic code + policy.toml,
  never a prompt.
- D6. Audit: append-only (DB triggers reject UPDATE/DELETE), secrets redacted,
  transactional with task transitions.
- D7. Task machine: 7 states (PENDING, PLANNING, RUNNING, WAITING_CONFIRMATION,
  COMPLETED, FAILED, CANCELLED), optimistic concurrency via version column.
  External-integration failure (NEXUS unavailable) => task COMPLETED with
  outcome UNAVAILABLE. FAILED reserved for internal failures with no safe
  response.
- D8. LLM: `LLMProvider` port first; `FakeLLMProvider` deterministic before
  any real adapter. LLM only produces a structured `NexusResponsePlan`
  (summary_key, tone, fact IDs); a deterministic pt-BR renderer owns all
  factual text. Unknown/invalid plan => audited deterministic fallback.
- D9. NEXUS consumed ONLY via versioned HTTP: GET /api/v1/equipment,
  GET /api/v1/equipment/{id}/summary,
  GET /api/v1/simulation/status?equipment_id={code}. No composite endpoint
  invented in NEXUS, no SQLite access, no business-logic replication.
- D10. "Real data" = data actually returned by the NEXUS API during execution.
  Simulation mode is disclosed as simulated, never presented as physical
  measurement. STALE never presented as current; EMPTY never as NORMAL.
- D11. Timeouts: connect 400ms, read 1200ms, pool 400ms, total deadline
  3500ms, 1 retry (idempotent GET only), backoff 100ms+jitter, circuit
  breaker 3 failures / 15s open / half-open probe, in-memory.
- D12. Freshness threshold: 10s (JARVIS_NEXUS_FRESHNESS_SECONDS), injectable
  clock.
- D13. Persistence: own SQLite (SQLAlchemy 2.x + aiosqlite + Alembic),
  4 tables (sessions, messages, tasks, audit_logs), 2 migrations, WAL + FK +
  busy_timeout. Startup only checks schema version; /ready 503 when behind.
- D14. Cancellation is first-class: CancelledError propagates, task ->
  CANCELLED, audit records it. Never swallowed.
- D15. Smoke test against a real NEXUS is manual, read-only, marked, never in
  CI. If NEXUS is not running on 127.0.0.1:8000, smoke = NOT TESTED.
- D16. Out of scope for Phase 1 (do not build): voice, vision, agents,
  automation, filesystem/shell tools, memory, vector DB, Redis/Kafka/Celery,
  K8s, plugins, web UI, public exposure, NEXUS writes, NEXUS changes.
- D17. Ruff: ignore TID252 (relative intra-package imports), UP042
  (`class X(str, Enum)` string enums), N818 (JarvisException naming), B008
  (FastAPI Depends()-in-defaults idiom), S101 (asserts in tests), DTZ005.
  S311 noqa on backoff jitter (not cryptographic); S110 noqa on two
  best-effort guards in the orchestrator. All are codebase conventions, not
  suppressions of real issues.
- D18. NEXUS adapter: httpx AsyncClient built with trust_env=False. Rationale:
  NEXUS is loopback-only by contract and must never egress through a proxy;
  on this machine proxy env vars are set (and one is malformed enough to
  crash client construction). Found during step 13 gating.
- D19. No auto-migration on startup. /ready returns 503 "schema behind"
  until `alembic upgrade head` runs (JARVIS_DATABASE_URL respected by
  migrations/env.py). Explicit beats magic for a local service.
- D20. AppState is built eagerly in create_app(), not in lifespan: ASGI
  transports used in tests (httpx ASGITransport) never run lifespan.
  Lifespan only logs readiness and releases resources on shutdown.
- D21. GET /version exposes `schema_version` (not `schema`): `schema`
  collides with pydantic v2's deprecated BaseModel.schema classmethod and
  breaks mypy strict.
- D22. "Never reference NEXUS" DB guards (config.Settings validator,
  persistence Database, migrations/env.py) now match on whole path
  components via shared `jarvis.config.references_nexus_database()` instead
  of a plain substring check. The substring version false-positived on
  pytest tmp dirs named after tests containing the word "nexus" (e.g.
  `test_nexus_offline_explicit_unavailable`). Still blocks any URL whose
  path contains a `nexus` directory or `nexus.*` database file. Found
  during step 14 test-suite build.
- D23. OpenAIProvider (internet egress) keeps trust_env=True to honor the
  system proxy, but falls back to trust_env=False with a loud warning when
  the proxy environment is malformed enough to crash httpx at construction
  (this machine's no_proxy contains a bare `::1` that breaks httpx's
  URLPattern). A malformed env must not be a cryptic startup crash; the
  NEXUS adapter keeps trust_env=False unconditionally (D18) because
  loopback must never egress via proxy.
- D24. Startup crash recovery is wired into the API lifespan (added during
  final validation, 2026-09-30): `mark_interrupted` existed as a pure
  function but nothing called it. `application/recovery.py` now scans
  non-terminal tasks at startup, marks each FAILED
  (current_step=process_interrupted) with optimistic-concurrency guard and
  an audited task.failed event. Runs only when /ready checks pass (schema
  at head). A task that died with its process can never resume; leaving it
  non-terminal would violate the terminal-state invariant.
- D25. The static no-coupling check (`test_no_nexus_imports`) now flags ANY
  import mentioning nexus outside our own package (relative or `jarvis.*`
  imports are ours by construction: contracts, ports, adapter,
  verification). The previous version only flagged lines also containing
  "workspace/nexus"/"lucaswbohrer" and missed a hypothetical bare
  `import nexus`. Runtime sys.modules check remains the real enforcement.
- D26. Memory items created or superseded by explicit user command are born
  ACTIVE, not PENDING: an explicit command is trusted provenance. PENDING
  is reserved for future flows (imports, proposed writes awaiting confirm).
- D27. Physical purge is a deliberate two-step: soft delete (tombstone)
  first, then `DELETE ...?purge=true`. Purging a non-deleted item is
  INPUT_INVALID. Provenance links (`superseded_by`) pointing at the purged
  item are cleared in the same transaction.
- D28. The FTS5 sync triggers use `DELETE FROM memory_fts WHERE
  rowid = old.rowid` + re-INSERT, keyed on the stable rowid of
  `memory_items` (the TEXT `item_id` rides along as an UNINDEXED column
  for the retrieve JOIN). The documented FTS5 special command
  `INSERT INTO ft(ft,...) VALUES('delete',...)` raises
  `sqlite3.OperationalError: SQL logic error` on SQLite 3.45.1 for a plain
  (non-external-content) FTS5 table — verified empirically; only
  `rebuild`/`integrity-check` work as special commands there.
- D29. `supersede` inserts the successor row BEFORE updating the
  predecessor's `superseded_by` link: the self-referencing FK rejects the
  link update first (FOREIGN KEY constraint failed). Same transaction, so a
  failed link still rolls the successor back.
- D30. The `_LEAD_STRIP_RE` word alternatives require a following
  separator or end-of-string (`(?=[\s:,\-—]|$)`) — without the lookahead
  the alternation ate the leading "a" of "apagar" ("apagar tudo" parsed
  as "pagar tudo").
- D31. Policy decisions and secret-block events are audited in their OWN
  transaction, outside the write transaction. Bug found in T14: on a
  policy DENY or a secret block, the enclosing write transaction rolled
  back and erased the `policy.decided`/`memory.write_blocked` audit rows
  — a denial left zero trail. Now contract → policy (durable audit) →
  scan (durable block audit) → persist (write + op audit atomic). An ALLOW
  whose write later fails is honest: the decision happened, the write did
  not.
- D32. Terminal memory statuses (superseded, expired, revoked, deleted)
  are immutable: they never transition to another status. Consequence:
  only active/pending items can be tombstoned and purged; a revoked item
  can never be physically erased (revocation is evidence, kept forever).
- D33. `supersede` inherits `kind` and `provenance` from the target; the
  caller supplies only the new title/content (plus optional confidence,
  sensitivity, valid_until).
- D34. Conversation tail reuses the existing
  `MessageRepository.list_by_session(limit=100)` (as the review mandates);
  the builder slices the last 20. The 100-message envelope is documented;
  a dedicated DESC query is deferred to future work.
- D35. `LLMRequest` gains an additive optional `context_sections` field
  (mandated by the review §12). No SCHEMA_VERSION bump: the request object
  is in-flight only, never persisted.
- D36. `MemoryCtx` carries `sensitivity` (the review §5 omits it, but the
  mandated sensitive-filter in §8.5 is impossible without it).
- D37. Deterministic preference → `detail_level` rule (brief hints win;
  e.g. "resposta curta" → BRIEF, "detalhado" → STANDARD) is the SINGLE
  permitted influence of memory on the response plan: presentation only,
  never facts. Enforced by the orchestrator for both the LLM plan and the
  fallback path; mirrored as rules 6–7 in the OpenAI system prompt.
- D38. Implicit context retrieval uses the raw read-only
  `memory_service.retrieve()` (never the authorized tool path): the
  orchestrator is trusted core, `memory.read` is statically ALLOW for
  `local_user`, and context assembly is not a tool invocation. No
  per-message policy audit is emitted for it.
- D39. Rewrote the 0003 `commitments` DDL to the exact review-§4 schema
  (`open/fulfilled/expired/cancelled`; added `detail`, `created_by`,
  `source_message_id`, `fulfilled_at`, `last_surfaced_at`; dropped the
  `memory_item_id` stub column): zero git commits exist, nothing was
  published, the real DB is still at revision 0002 (0003 never applied to
  real data), and tests run on temp DBs only. This is a pre-release
  correction, not a new migration (new migrations are forbidden mid-task).
- D40. Commitment mutations do NOT go through `policy.decide` (review §9.1
  is a closed capability list): authorization is explicit-user-origin only
  (`origin="user_explicit_command"`), intents are recognized from user text
  only, and the HTTP endpoints are loopback-only. Plans carry
  `capability_set=["none"]` since no policy capability exists for
  commitments.
- D41. Deterministic pt-BR due-date parser: "amanhã"/"hoje"/weekdays/
  "dd/mm"; end-of-day 23:59:59 UTC; an unparseable date expression stays in
  the title with `due_at=None` (never a fabricated deadline).
- D42. Surfacing = open commitments due within 24h (`overdue` flag when past
  due); `last_surfaced_at` + the `commitment.surfaced` audit are stamped in
  the SAME tx as the response persist, so a charge is never shown twice nor
  lost between display and stamp.
- D43. First-boot migrations create the sqlite file's parent dir themselves
  (`migrations/env.py::_ensure_parent_dir`, F3.4 clean-room fix): a fresh
  clone has no `./data/`, and alembic runs before the app's own mkdir. Path
  resolution mirrors `Database.db_file()` exactly
  (`sqlite+aiosqlite:///./data/jarvis.db` -> `./data/jarvis.db`, relative to
  CWD -- never urlsplit's absolute `/data/jarvis.db`); the NEXUS-database
  guard still runs first.
- D44. Empty optional env vars mean "unset": `JARVIS_LLM_INPUT_PRICE_PER_1M_USD=`
  (as shipped in `.env.example`) parses to `None` instead of crashing
  pydantic float parsing at first boot (F3.4 clean-room fix).
- D45. Audit/message list queries tie-break timestamp ties with SQLite
  `rowid` (true insertion order), not the random-UUID `id`: on Windows the
  OS clock granularity (~15.6 ms) collapses causally-ordered events into one
  timestamp and the old tiebreak produced arbitrary orders. No migration —
  rowid is intrinsic to these append-only rowid tables (F3.4.1 Windows
  portability fix).
- D46. Test `repos` fixture disposes its engine in teardown: aiosqlite worker
  threads must not outlive the test's event loop (`RuntimeError: Event loop
  is closed`). `Database.close()` is idempotent, so tests that already
  close `repos["db"]` are unaffected (F3.4.1).
- D47. No file read in the repo may depend on the OS default codec: test
  helpers read/write with explicit `encoding="utf-8"` (Windows default is
  cp1252). Production code already reads TOML in binary mode (F3.4.1).

## Step gates

Each of the 17 implementation steps must be green before the next begins.
If policy or audit is not operational, the NEXUS adapter stays disconnected
from the orchestrator.
