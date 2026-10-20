# Configuration

All configuration is through environment variables, normally set in a `.env` file at the repo root.

- [`.env` and `.env.example`](#env-and-envexample)
- [Environment variables](#environment-variables)
- [Switching ClickHouse targets](#switching-clickhouse-targets)
- [Model selection](#model-selection)
- [Things that are not configurable](#things-that-are-not-configurable)

---

## `.env` and `.env.example`

| File | In git? | Purpose |
|------|---------|---------|
| `.env.example` | yes | Template with every variable, defaults for the public ClickHouse playground, and a commented-out block for your own ClickHouse Cloud service |
| `.env` | **no** (`.gitignore`) | Your real settings, including `GOOGLE_API_KEY` and any ClickHouse password |

Create it once:

```powershell
copy .env.example .env        # macOS/Linux: cp .env.example .env
```

then set `GOOGLE_API_KEY`.

**How it is loaded.** Every entry point calls `python-dotenv`'s `load_dotenv()`:

| Code | Call | Finds `.env` by |
|------|------|-----------------|
| `Solution/movie_agent/agent.py`, `__main__.py` | `load_dotenv()` | walking up from the script's folder to the repo root |
| `Solution/sushmita_agent/agent.py`, `__main__.py` | `load_dotenv()` | same |
| `Solution/host_agent/host/agent.py`, `remote_agent_connection.py` | `load_dotenv()` | same, from `host/` |
| `clickhouse/load_imdb.py` | `load_dotenv()` | same |
| `tests/check_movie_tools.py` | `load_dotenv(ROOT / ".env")` | explicit repo-root path |
| Notebooks | `load_dotenv()` | walking up from the notebook kernel's working directory |

`load_dotenv()` does **not** override variables that are already set in the process environment, so an exported variable wins over `.env`. That is handy for one-off overrides:

```powershell
$env:MOVIE_MIN_CAST_SIZE = "50"; python tests/check_movie_tools.py
```

Comments after values (`GEMINI_MODEL=gemini-3.8-flash   # ...`) are stripped by python-dotenv; an empty value (`MOVIE_AGENT_MODEL=`) is read as an empty string, which the code treats as "not set" where it uses `os.getenv(...) or default`.

Never commit `.env` or API key files; `.gitignore` also excludes `clickhouse-api-key-admin.txt` and `*api-key*.txt`.

---

## Environment variables

### Gemini

| Variable | Default | Read by | Notes |
|----------|---------|---------|-------|
| `GOOGLE_API_KEY` | none | Movie Agent `__main__.py` (startup check), Sushmita `agent.py` and `__main__.py` (required, passed to CrewAI's `LLM`), and implicitly by google-genai for the Movie and Host agents | Get one at https://aistudio.google.com/apikey. The project needs billing or credits. |
| `GOOGLE_GENAI_USE_VERTEXAI` | `FALSE` | Movie Agent `__main__.py`; google-genai (ADK) | When exactly `TRUE`, the Movie Agent skips the API-key check and google-genai uses Vertex AI credentials. The Sushmita Agent always requires `GOOGLE_API_KEY`. |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Host `agent.py`, Sushmita `agent.py`, Movie `agent.py` (fallback) | Model for all agents. The Sushmita Agent prefixes it with `gemini/` for CrewAI if needed. |
| `MOVIE_AGENT_MODEL` | value of `GEMINI_MODEL` | Movie `agent.py` | Overrides the model for the Movie Agent only. |

### ClickHouse

Read by `Solution/movie_agent/agent.py`, `clickhouse/load_imdb.py` and both notebooks, with identical defaults and parsing.

| Variable | Code default | `.env.example` (playground) | Notes |
|----------|--------------|-----------------------------|-------|
| `CLICKHOUSE_HOST` | `localhost` | `sql-clickhouse.clickhouse.com` | Hostname only, no scheme |
| `CLICKHOUSE_PORT` | `8443` if secure, else `8123` | `443` | HTTP(S) interface port, not the native 9000 |
| `CLICKHOUSE_USERNAME` | `default` | `demo` | Stored in the code as `CLICKHOUSE_USER` |
| `CLICKHOUSE_PASSWORD` | empty | empty | |
| `CLICKHOUSE_DATABASE` | `imdb` | `imdb` | Default database for queries; `load_imdb.py` creates it under this name |
| `CLICKHOUSE_SECURE` | `false` | `true` | `1`, `true` or `yes` (any case) enable TLS |

Port resolution: `int(os.getenv("CLICKHOUSE_PORT") or (8443 if CLICKHOUSE_SECURE else 8123))`, so an empty `CLICKHOUSE_PORT=` falls back to the default.

### Movie ranking

| Variable | Default | Read by | Notes |
|----------|---------|---------|-------|
| `MOVIE_MIN_CAST_SIZE` | `30` | Movie `agent.py` (`MIN_CAST_SIZE`) | Minimum number of credited actors for `recommend_movies_by_genre` and `recommend_movies_by_year`. See [ranking](MOVIE_AGENT_AND_CLICKHOUSE.md#ranking-strategy). Must be an integer. |

`FEATURE_CAST_SIZE = 10` (feature films first in actor/director lists) is a constant in `agent.py`, not an environment variable.

### Sushmita Agent

| Variable | Default | Read by | Notes |
|----------|---------|---------|-------|
| `HOST_OVERRIDE` | `http://localhost:10006/` | Sushmita `__main__.py` | URL advertised in the Sushmita Agent card's `supported_interfaces`. The Host sends its JSON-RPC requests to this URL, so set it when the agent is reached through a proxy or tunnel. It does not change where the server listens (always `localhost:10006`). |

---

## Switching ClickHouse targets

Only the Movie Agent (and `load_imdb.py`, the notebooks) talk to ClickHouse. Restart the Movie Agent after changing these values; they are read at import time.

### Public SQL playground (default)

No setup, IMDB already loaded, read-only and shared:

```ini
CLICKHOUSE_HOST=sql-clickhouse.clickhouse.com
CLICKHOUSE_PORT=443
CLICKHOUSE_USERNAME=demo
CLICKHOUSE_PASSWORD=
CLICKHOUSE_DATABASE=imdb
CLICKHOUSE_SECURE=true
```

Do not run `load_imdb.py` against it (the `demo` user cannot create tables).

### Your own ClickHouse Cloud service

Comment out the playground block in `.env`, uncomment the Cloud block and fill it in:

```ini
CLICKHOUSE_HOST=<id>.<region>.<provider>.clickhouse.cloud   # from the console's "Connect" dialog
CLICKHOUSE_PORT=8443
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=your_password
CLICKHOUSE_DATABASE=imdb
CLICKHOUSE_SECURE=true
```

Then load the data once: `python clickhouse/load_imdb.py` (the service needs outbound internet access to read from S3; Cloud has it by default). Verify with `python tests/check_movie_tools.py`.

### Local ClickHouse server

```ini
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=
CLICKHOUSE_DATABASE=imdb
CLICKHOUSE_SECURE=false
```

Then `python clickhouse/load_imdb.py`. The server itself downloads the files from `https://datasets-documentation.s3.eu-west-3.amazonaws.com/imdb/`, so it needs internet access.

### Using a different database name

Set `CLICKHOUSE_DATABASE=movies_demo` (for example). `load_imdb.py` rewrites `imdb` in `schema.sql` to that name, and the Movie Agent connects with it as the default database (its queries use unqualified table names).

---

## Model selection

| Agent | Model used | How it calls Gemini |
|-------|-----------|---------------------|
| Host Agent | `GEMINI_MODEL` or `gemini-3.8-flash` | ADK `Agent(model=...)` -> google-genai |
| Movie Agent | `MOVIE_AGENT_MODEL` or `GEMINI_MODEL` or `gemini-3.8-flash` | ADK `LlmAgent(model=...)` -> google-genai |
| Sushmita Agent | `gemini/` + (`GEMINI_MODEL` or `gemini-3.8-flash`) | CrewAI `LLM(model=..., api_key=GOOGLE_API_KEY)` |

Examples:

```ini
# All agents on one model
GEMINI_MODEL=gemini-3.8-flash

# A stronger model just for the agent that does tool-heavy SQL work
MOVIE_AGENT_MODEL=<another Gemini model id>
```

- Use a model id your key can access. If a model is retired (as `gemini-2.0-flash` was on 2026-06-01), requests fail; the remote agents report this as `TASK_STATE_FAILED` with the error text.
- For the Sushmita Agent you may also write the CrewAI form (`GEMINI_MODEL=gemini/<id>`); the prefix is not added twice. The ADK agents expect the plain id, so prefer the plain form.
- Model changes need a restart of the affected processes (`adk web` included).

---

## Things that are not configurable

These are constants in code; change the code if you need different values:

| Setting | Value | Where |
|---------|-------|-------|
| Movie Agent bind address | `localhost:10005` | `Solution/movie_agent/__main__.py`, `main()` |
| Sushmita Agent bind address | `localhost:10006` | `Solution/sushmita_agent/__main__.py`, `main()` |
| Remote agent URLs used by the Host | `http://localhost:10005`, `http://localhost:10006` | `friend_agent_urls` in `_get_initialized_host_agent_sync()` |
| Host web UI port | `--port 8000` on the `adk web` command line | your command |
| A2A client timeout | 30 s (`httpx.AsyncClient(timeout=30)`) | `RemoteAgentConnections.__init__` |
| `FEATURE_CAST_SIZE` | 10 | `Solution/movie_agent/agent.py` |
| Excluded genres | `Short`, `Documentary`, `Adult` | SQL in `recommend_movies_by_genre` / `recommend_movies_by_year` |
| Sushmita's genres and watched list | `MOVIE_GENRE_PREFERENCES`, `WATCHED_MOVIES` | `Solution/sushmita_agent/agent.py` |
