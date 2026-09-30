# JARVIS — Local Development

How to run JARVIS from zero on a clean machine. Every command below is the
project's real mechanism (checked against `pyproject.toml`, `alembic.ini`,
`src/jarvis/__main__.py` and `.env.example`) — nothing is invented.

Requirements: **Python 3.12**, `git`, internet access for `pip`.

## Linux / macOS

```bash
# 1. Clone
git clone https://github.com/LucaswBohrer/jarvis.git
cd jarvis

# 2. Virtual environment (Python 3.12)
python3.12 -m venv .venv
source .venv/bin/activate

# 3. Install (runtime + dev tools: pytest, ruff, mypy)
python -m pip install --upgrade pip
pip install -e ".[dev]"

# 4. Configure — copy the example and edit the values you need.
#    Never commit a real .env (it is git-ignored).
cp .env.example .env

# 5. Database — create from zero with the project's migrations.
#    This creates ./data/jarvis.db locally (also git-ignored).
alembic upgrade head
alembic current          # expected: 0004 (head)

# 6. Run — the official entrypoint (loopback only, 127.0.0.1:8123)
python -m jarvis
```

Open in the browser:

- UI: <http://127.0.0.1:8123>
- Health: <http://127.0.0.1:8123/health>
- Readiness: <http://127.0.0.1:8123/ready> (must be `{"ready":true,"reason":"ok"}`)

## Windows (PowerShell)

```powershell
# 1. Clone
git clone https://github.com/LucaswBohrer/jarvis.git
cd jarvis

# 2. Virtual environment (Python 3.12)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install
python -m pip install --upgrade pip
pip install -e ".[dev]"

# 4. Configure
Copy-Item .env.example .env
# edit .env in notepad if needed

# 5. Database
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\alembic current   # expected: 0004 (head)

# 6. Run
python -m jarvis
```

Same URLs as above: <http://127.0.0.1:8123>.

## Quality gates

```bash
# Linux/macOS
python -m pytest tests/ -q
ruff check .
ruff format --check .
python -m mypy src
```

```powershell
# Windows
python -m pytest tests/ -q
ruff check .
ruff format --check .
python -m mypy src
```

## LLM provider

The default `.env` uses the deterministic fake provider — no key, no
internet needed, all 300 tests run with it:

```text
JARVIS_LLM_PROVIDER=fake
```

To use the real provider (OpenAI-compatible REST, no SDK):

```text
JARVIS_LLM_PROVIDER=openai
JARVIS_LLM_MODEL=gpt-4o-mini
JARVIS_OPENAI_API_KEY=<redacted>   # your key, never committed
```

Notes:

- The variable name is `JARVIS_OPENAI_API_KEY` (not `OPENAI_API_KEY`).
- `provider=openai` without key+model fails **fast at startup** with a clear
  error — it never crashes at import, and `fake` keeps working without a key.
- The key never leaves the backend: it is sent only as the `Authorization`
  header to the provider API. It never appears in the web UI, HTTP
  responses, logs, audit events or exceptions (covered by tests in
  `tests/unit/test_llm_provider_security.py`).
- The adapter does **not** retry failed calls. On any provider failure the
  orchestrator answers with the audited deterministic fallback instead of
  inventing a response.

### LLM smoke test (not part of pytest)

```bash
# No key needed: validates the real HTTP wire path against a local stub
python scripts/smoke_llm.py --stub

# Real provider (requires the env vars above); fails safe without them
JARVIS_LLM_PROVIDER=openai \
JARVIS_LLM_MODEL=gpt-4o-mini \
JARVIS_OPENAI_API_KEY=<redacted> \
    python scripts/smoke_llm.py
```

```powershell
# Windows
python scripts/smoke_llm.py --stub
$env:JARVIS_LLM_PROVIDER="openai"; $env:JARVIS_LLM_MODEL="gpt-4o-mini"
$env:JARVIS_OPENAI_API_KEY="<sua-chave>"; python scripts/smoke_llm.py
```

## First-run checklist

After `python -m jarvis`, verify the whole loop:

1. `GET /health` → `{"status":"ok","version":"0.1.0"}`
2. `GET /ready` → `{"ready":true,"reason":"ok"}`
3. `GET /` → the web shell
4. `POST /api/v1/sessions` → `201 {"id": "..."}`
5. `POST /api/v1/sessions/{id}/messages` with `{"content": "..."}`
6. Reload the page → history is restored (`GET /api/v1/sessions/{id}/messages`)

## NEXUS

JARVIS never starts NEXUS. If NEXUS is not running at
`http://127.0.0.1:8000`, questions about it are answered honestly as
unavailable — never invented. The boundary is HTTP GET on `/api/v1/` only,
loopback only.

## What is NOT versioned

`data/jarvis.db`, `backups/`, `.env` — all git-ignored. A fresh clone starts
with no database; `alembic upgrade head` creates it.
