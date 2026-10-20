# A2A Protocol with ClickHouse

Three AI agents that talk to each other over the [A2A protocol](https://a2a-protocol.org) to plan a movie night:

| Agent | Built with | Port | What it does |
|-------|-----------|------|--------------|
| **Movie Agent** | Google ADK | 10005 | Recommends movies from the [IMDB dataset](https://clickhouse.com/docs/getting-started/example-datasets/imdb) stored in ClickHouse |
| **Sushmita Agent** | CrewAI | 10006 | Knows Sushmita's availability, favorite genres and already-watched movies |
| **Host Agent** | Google ADK | 8000 (web UI) | Talks to you, asks the other two agents, and confirms / cancels movie plans |

```
            you (browser, http://localhost:8000)
                          │
                    ┌─────▼──────┐
                    │ Host Agent │  (adk web)
                    └──┬──────┬──┘
                 A2A   │      │   A2A
          ┌────────────▼┐    ┌▼──────────────┐
          │ Movie Agent │    │ Sushmita Agent│
          │   :10005    │    │    :10006     │
          └──────┬──────┘    └───────────────┘
                 │ SQL
          ┌──────▼──────┐
          │ ClickHouse  │  imdb database
          └─────────────┘
```

All three agents use Gemini, so you need a Gemini API key with available quota.

## Documentation
This README gets you running. For how everything works, see the in-depth guides in [`docs/`](docs/README.md):

| Guide | What it covers |
|-------|----------------|
| [Architecture](docs/ARCHITECTURE.md) | Components, ports, the end-to-end request flow, sequence diagram, state and error handling |
| [A2A protocol](docs/A2A_PROTOCOL.md) | Agent cards, tasks, messages and artifacts, JSON-RPC examples, a2a-sdk 0.x → 1.x changes |
| [Movie Agent and ClickHouse](docs/MOVIE_AGENT_AND_CLICKHOUSE.md) | The IMDB dataset, each tool's SQL, the ranking strategy, Neo4j → ClickHouse mapping |
| [Host and Sushmita agents](docs/HOST_AND_SUSHMITA_AGENTS.md) | How the Host orchestrates, the plan tools, the CrewAI agent |
| [Development](docs/DEVELOPMENT.md) | The `Problem/` exercise, adding tools or agents, testing, upgrading, Windows notes |
| [Configuration](docs/CONFIGURATION.md) | Every environment variable, playground vs Cloud vs local ClickHouse, model selection |

The code is commented too: every module starts with a docstring explaining its role, and the SQL queries
and A2A plumbing are explained inline.

---

## Quick start

Commands are shown for **Windows (PowerShell)**; macOS/Linux equivalents are in the comments.
Run everything from the repo root unless a step says otherwise.

### 1. Prerequisites
- **Python 3.11, 3.12 or 3.13.** Not 3.14: crewai doesn't support it yet.
- **A Gemini API key:** get one at https://aistudio.google.com/apikey. The project needs billing or credits.
- *(Optional)* **[uv](https://docs.astral.sh/uv/)** for faster installs.

No ClickHouse setup is needed: by default the demo uses ClickHouse's public SQL playground, which already has the IMDB data.

### 2. Create the virtualenv and install dependencies
```powershell
# with uv (recommended)
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt

# or with plain Python
py -3.13 -m venv .venv                    # macOS/Linux: python3.13 -m venv .venv
.venv\Scripts\Activate.ps1                # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```
Keep the virtualenv at `<repo>\.venv`. On Windows, crewai fails to import from long, deeply nested paths.

### 3. Configure `.env`
```powershell
copy .env.example .env                    # macOS/Linux: cp .env.example .env
```
Open `.env` and set `GOOGLE_API_KEY`. The ClickHouse settings already point at the public playground.

### 4. Activate the virtualenv
Do this in **every** terminal you open for this project:
```powershell
.venv\Scripts\Activate.ps1                # macOS/Linux: source .venv/bin/activate
```
If PowerShell says running scripts is disabled, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use `cmd` and `.venv\Scripts\activate.bat`.

### 5. Check the database connection (no Gemini needed)
```powershell
python tests/check_movie_tools.py
```
You should see `6/6 checks passed`. If you don't, fix ClickHouse before going further (see [Troubleshooting](#troubleshooting)).

### 6. Start the agents: three terminals, in this order
**Terminal 1: Movie Agent**
```powershell
cd Solution/movie_agent
python .
```
Wait for `Uvicorn running on http://localhost:10005`.

**Terminal 2: Sushmita Agent**
```powershell
cd Solution/sushmita_agent
python .
```
Wait for `Uvicorn running on http://localhost:10006`.

**Terminal 3: Host Agent**
```powershell
cd Solution/host_agent
adk web --port 8000
```
Start the Host Agent last: it fetches the other two agents' cards when it starts.

### 7. Use it
Open **http://localhost:8000**, choose **host** in the app dropdown (top left), and chat:
- `What sci-fi movies would you recommend?`
- `What genres does Sushmita like, and is she free this weekend?`
- `Plan a movie night with Sushmita: pick a film she hasn't seen in a genre she likes.`
- `Confirm the plan` · `Show my movie plans` · `Cancel the plan`

The **Events** tab shows every tool call and every A2A request the Host Agent sends.

To stop everything, press `Ctrl+C` in each terminal.

---

## Testing each layer
If something doesn't work, test the layers from the bottom up. The first step that fails tells you which part is broken.

| # | Tests | Command | Expected |
|---|-------|---------|----------|
| 1 | ClickHouse + movie tools | `python tests/check_movie_tools.py` | `6/6 checks passed` |
| 2 | Movie Agent server is up | open http://localhost:10005/.well-known/agent-card.json | JSON card named `Movie Agent` |
| 3 | Sushmita Agent server is up | open http://localhost:10006/.well-known/agent-card.json | JSON card named `Sushmita Agent` |
| 4 | Movie Agent + Gemini | `python tests/send_a2a_message.py "Recommend 3 sci-fi movies"` | `State: TASK_STATE_COMPLETED` + movies |
| 5 | Sushmita Agent + Gemini | `python tests/send_a2a_message.py "What genres do you like?" --url http://localhost:10006/` | `State: TASK_STATE_COMPLETED` + genres |
| 6 | Everything | chat at http://localhost:8000 (step 7 above) | answers that use both agents |

Steps 2–6 need the agents running (Quick start, step 6). In steps 4–5, a `TASK_STATE_FAILED` result prints the underlying error on the next line.

To check the cards from a terminal, use `curl.exe` in Windows PowerShell; plain `curl` there is an alias for `Invoke-WebRequest`.

---

## Using your own ClickHouse server
The public playground is read-only and shared. To use your own ClickHouse Cloud service (or a local server):

1. In `.env`, comment out the playground block and fill in the "your own Cloud service" block:
   - **Host:** shown in the ClickHouse Cloud console under *Connect*.
   - **Port:** `8443`.
   - **User and password:** `default` plus the password shown when the service was created.
   - **Local server:** use port `8123` and `CLICKHOUSE_SECURE=false`.
2. Load the IMDB dataset (~5.5M rows in total; safe to re-run):
   ```powershell
   python clickhouse/load_imdb.py
   ```
   The ClickHouse server downloads the files directly from ClickHouse's public S3 bucket, so it needs internet access.
3. Re-run `python tests/check_movie_tools.py`.

---

## The `Problem/` exercise
`Solution/` is the complete code. `Problem/` is the same project with `# TODO` gaps to fill in:
- **Movie Agent:** the ClickHouse client, the genre query and `create_agent()` (`agent.py`), and the agent card
  and skill (`__main__.py`).
- **Sushmita Agent:** the agent card, the server wiring, and the CrewAI agent.
- **Host Agent:** the A2A message and the remote agent URLs.

Every `# TODO` has a hint comment above it, and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) lists them all.
Once the gaps are filled, run it exactly like the solution, but with `Problem/...` in step 6.

---

## Notebooks
Install Jupyter into the virtualenv and open the notebooks. They read the same `.env`.
```powershell
pip install jupyterlab                    # or: uv pip install --python .venv jupyterlab
jupyter lab Notebook/
```
- `clickhouse_connection.ipynb`: connects to ClickHouse, runs sample queries, and loads results into pandas.
- `clickhouse_db_exploration.ipynb`: inspects the schema and explores the IMDB data.

---

## Reference

### Environment variables (`.env`)
| Variable | Default | Purpose |
|----------|---------|---------|
| `GOOGLE_API_KEY` | none | Gemini API key (required unless using Vertex AI) |
| `GOOGLE_GENAI_USE_VERTEXAI` | `FALSE` | `TRUE` to use Vertex AI credentials for the ADK agents (the Sushmita Agent still needs `GOOGLE_API_KEY`) |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model used by all agents |
| `MOVIE_AGENT_MODEL` | `GEMINI_MODEL` | Model override for the Movie Agent only |
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `CLICKHOUSE_PORT` | `8123` (`8443` if secure) | HTTP(S) port |
| `CLICKHOUSE_USERNAME` | `default` | ClickHouse user |
| `CLICKHOUSE_PASSWORD` | empty | ClickHouse password |
| `CLICKHOUSE_DATABASE` | `imdb` | Database holding the IMDB tables |
| `CLICKHOUSE_SECURE` | `false` | `true` for TLS (ClickHouse Cloud / playground) |
| `MOVIE_MIN_CAST_SIZE` | `30` | Minimum credited cast for genre/year recommendations |
| `HOST_OVERRIDE` | none | Public URL to advertise in the Sushmita Agent card |

### The IMDB data
| Table | Rows | Columns |
|-------|------|---------|
| `movies` | ~388k | `id`, `name`, `year`, `rank` (IMDb rating, 0 = unrated) |
| `actors` | ~818k | `id`, `first_name`, `last_name`, `gender` |
| `directors` | ~87k | `id`, `first_name`, `last_name` |
| `roles` | ~3.4M | `actor_id`, `movie_id`, `role` |
| `movie_directors` | ~371k | `director_id`, `movie_id` |
| `genres` | ~395k | `movie_id`, `genre` |

- **Coverage:** movies from 1888 to 2008. Only about 67k of them have a rating.
- **Titles:** the dataset moves a leading article to the end, so "The Matrix" is stored as `Matrix, The`. The agent converts in both directions, so you can ask for "The Matrix".
- **Names:** namesakes are told apart with suffixes like `Chris (I)`. The agent strips these for matching and display.
- **Ranking:** there are no vote counts, so cast size stands in for popularity.
  - Genre and year recommendations only include rated movies with at least `MOVIE_MIN_CAST_SIZE` credited actors, and leave out shorts, documentaries and adult titles unless you ask for that genre.
  - Actor and director lists show feature films first.

### Versions
google-adk 2.12 · a2a-sdk 1.2 · crewai 1.15 · clickhouse-connect 1.11 · Python 3.11–3.13

### Project layout
```
Solution/
  movie_agent/        __main__.py (A2A server :10005), agent.py (ClickHouse tools), agent_executor.py
  sushmita_agent/     __main__.py (A2A server :10006), agent.py (CrewAI), agent_executor.py
  host_agent/host/    agent.py (root_agent + A2A client), movie_tools.py, remote_agent_connection.py
Problem/              same layout, with # TODO gaps
clickhouse/           schema.sql, load_imdb.py
tests/                check_movie_tools.py, send_a2a_message.py
Notebook/             ClickHouse notebooks
docs/                 in-depth documentation
```

---

## Troubleshooting
| Symptom | Fix |
|---------|-----|
| `402 RESOURCE_EXHAUSTED` / "prepayment credits are depleted" | The Gemini key is valid but its project has no credit. Top up at https://ai.studio/projects. `tests/check_movie_tools.py` still works without credit. |
| `401` / `403` / "API key not valid" from Gemini | Check `GOOGLE_API_KEY` in `.env`. |
| ClickHouse `Authentication failed` | Wrong `CLICKHOUSE_USERNAME` / `CLICKHOUSE_PASSWORD`. The playground uses `demo` with an empty password. |
| ClickHouse connection refused / timeout | Check `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT` and `CLICKHOUSE_SECURE`. For your own server, run `load_imdb.py` so the `imdb` database exists. |
| `ModuleNotFoundError` inside `crewai` (Windows) | The virtualenv path is too long. Recreate it as `<repo>\.venv`. |
| `No module named ...` when starting an agent | The virtualenv isn't active in that terminal (Quick start, step 4). |
| `Address already in use` / port 10005, 10006 or 8000 busy | An earlier run is still going. Stop it with `Ctrl+C`, or close that terminal. |
| Host Agent can't reach the other agents | Start the Movie and Sushmita agents **before** `adk web`, then check rows 2–3 of [Testing each layer](#testing-each-layer). |
| `Method not found` from an A2A server | a2a-sdk 1.x calls the method `SendMessage` (not `message/send`). |
| No **host** in the ADK web dropdown | Run `adk web` from inside `Solution/host_agent`. |
