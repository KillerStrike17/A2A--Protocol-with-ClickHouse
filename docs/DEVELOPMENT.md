# Development guide

- [Project layout](#project-layout)
- [Problem vs Solution: the exercise](#problem-vs-solution-the-exercise)
- [Running from source](#running-from-source)
- [Testing](#testing)
- [Adding a tool to the Movie Agent](#adding-a-tool-to-the-movie-agent)
- [Adding a new remote agent to the Host](#adding-a-new-remote-agent-to-the-host)
- [Notebooks](#notebooks)
- [Upgrading dependencies](#upgrading-dependencies)
- [Windows notes](#windows-notes)

---

## Project layout

```
.
├── .env.example                 configuration template (copy to .env)
├── requirements.txt             google-adk, a2a-sdk[http-server], crewai[tools], clickhouse-connect, ...
├── Solution/                    complete, working code
│   ├── movie_agent/             A2A server :10005, run with `python .` from this folder
│   │   ├── __main__.py          agent card + ADK Runner + Starlette/uvicorn
│   │   ├── agent.py             ClickHouse config, 6 SQL tools, create_agent()
│   │   └── agent_executor.py    MovieAgentExecutor, A2A <-> GenAI part converters
│   ├── sushmita_agent/          A2A server :10006, run with `python .` from this folder
│   │   ├── __main__.py          agent card + Starlette/uvicorn
│   │   ├── agent.py             calendar/preferences data, CrewAI tools, SchedulingAgent
│   │   └── agent_executor.py    SchedulingAgentExecutor
│   └── host_agent/              run `adk web` from this folder
│       └── host/
│           ├── __init__.py      exports root_agent for adk web
│           ├── agent.py         HostAgent, send_message, root_agent
│           ├── movie_tools.py   confirm/get/cancel movie plan (in-memory)
│           └── remote_agent_connection.py   a2a-sdk client wrapper
├── Problem/                     same layout with # TODO gaps
├── clickhouse/
│   ├── schema.sql               IMDB tables
│   └── load_imdb.py             create schema + load from S3 (own server only)
├── tests/
│   ├── check_movie_tools.py     ClickHouse + tools, no LLM
│   └── send_a2a_message.py      raw JSON-RPC SendMessage to a running agent
├── Notebook/                    ClickHouse connection and exploration notebooks
└── docs/                        this documentation
```

The agent folders are **not** installable packages: `__main__.py` uses flat imports (`from agent import create_agent`, `from agent_executor import ...`), so each agent must be started from inside its own folder with `python .` (Python adds that folder to `sys.path`). `tests/check_movie_tools.py` imports `Solution/movie_agent/agent.py` the same way by inserting the folder into `sys.path`.

---

## Problem vs Solution: the exercise

`Solution/` is the reference implementation. `Problem/` is a copy with gaps marked `# TODO`. The files with gaps do not run (several are not even valid Python) until every TODO in them is filled in. The `agent_executor.py` files and `remote_agent_connection.py` are complete in both trees, and `Problem/host_agent/host/movie_tools.py` is complete (only trivially different from the solution).

To work on the exercise, fill in the TODOs below, then run exactly as in the README but from `Problem/...` folders (`cd Problem/movie_agent; python .`, and so on).

### Movie Agent: `Problem/movie_agent/agent.py`

| Location | What to write |
|----------|---------------|
| `get_clickhouse_client()`: `client = # TODO` | Create the client with `clickhouse_connect.get_client(...)` from `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE`, `CLICKHOUSE_SECURE` |
| `get_clickhouse_client()`: `# TODO` inside `try:` | Check connectivity: `if not client.ping(): raise ConnectionError(...)` |
| `get_clickhouse_client()`: `# TODO` inside `except:` | Close the client before re-raising (`client.close()`) |
| `recommend_movies_by_genre()`: `query = # TODO` | The genre SQL: rated movies in the genre, excluding Short/Documentary/Adult unless requested, cast size >= `{min_cast:UInt32}`, ordered by rank then cast size, `LIMIT {limit:UInt32}`. Use `{genre:String}` parameters, not string formatting. See [the genre query](MOVIE_AGENT_AND_CLICKHOUSE.md#recommend_movies_by_genregenre-limit10) |
| `recommend_movies_by_genre()`: `result = # TODO` | `client.query(query, parameters={"genre": ..., "min_cast": MIN_CAST_SIZE, "limit": int(limit)})` |
| `recommend_movies_by_genre()`: `title`, `year`, `rating`, `movie_info` (x2) | Read `record["title"]` (through `to_display_title`), `record["year"]`, `record["rating"]` with fallbacks `"Unknown"` / `"N/A"`, and format `- <title> (<year>)` + ` [Rating: <rating>]` |
| `create_agent()`: `model`, `name`, `instruction`, `tools` | `LlmAgent(model=MOVIE_AGENT_MODEL, name="Movie_Agent", instruction=<role, data description, which tool for what>, tools=[all six tool functions])` |

The other five tools are already complete and are good references.

### Movie Agent: `Problem/movie_agent/__main__.py`

| Location | What to write |
|----------|---------------|
| `AgentSkill(id, name, description, tags, examples)` | The `movie_recommendations` skill: a name, description, tag list and example prompts |
| `AgentCard(name, description)` | `name="Movie Agent"` (the Host's instruction refers to this exact name) and a description |
| `AgentInterface(url=...)` | `f"http://{host}:{port}/"` |
| `default_input_modes`, `default_output_modes` | `["text/plain"]` |
| `capabilities`, `skills` | the `capabilities` object built above, `[skill]` |

### Sushmita Agent: `Problem/sushmita_agent/agent.py`

| Location | What to write |
|----------|---------------|
| `SchedulingAgent.__init__`: `LLM(model, api_key)` | `CREWAI_GEMINI_MODEL`, `os.getenv("GOOGLE_API_KEY")` |
| `Agent(role, goal, backstory)` | Text describing a personal assistant for scheduling and movie preferences, mentioning the two tools by name |
| `Agent(tools, llm)` | `[AvailabilityTool(), MoviePreferencesTool()]`, `self.llm` |
| `invoke()`: `task_description` | Prompt that includes the user's question, today's date (`date.today()`), and which tool to use for availability vs. preference questions |
| `invoke()`: `expected_output` | Description of a polite, concise answer |
| `Crew(agents, tasks, process)` | `[self.scheduling_assistant]`, `[check_availability_task]`, `Process.sequential` |

### Sushmita Agent: `Problem/sushmita_agent/__main__.py`

| Location | What to write |
|----------|---------------|
| `host = # TODO`, `port = # TODO` | `"localhost"`, `10006` (the Host Agent's URL list expects this port) |
| `AgentCard(name, description)` | `name="Sushmita Agent"` (exact name used by the Host) and a description of availability + movie preferences |
| `AgentInterface(url=...)` | `agent_host_url` (which honours `HOST_OVERRIDE`) |
| `capabilities`, `skills` | `capabilities`, `[skill1, skill2]` |
| `DefaultRequestHandler(agent_executor, agent_card)` | `SchedulingAgentExecutor()`, `agent_card` |

### Host Agent: `Problem/host_agent/host/agent.py`

| Location | What to write |
|----------|---------------|
| `HostAgent.send_message`: `Message(parts, message_id, context_id)` | `parts=[Part(text=task)]`, `message_id=message_id`, `context_id=context_id` (a2a-sdk 1.x protobuf style, see [A2A_PROTOCOL.md](A2A_PROTOCOL.md#messages-and-parts)) |
| `_get_initialized_host_agent_sync`: `friend_agent_urls = [ # TODO ]` | `"http://localhost:10005"`, `"http://localhost:10006"` |

### Checking your work

1. `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" Problem/movie_agent/agent.py` (repeat per file) to catch leftover syntax gaps.
2. Point `tests/check_movie_tools.py` at your code by temporarily changing `"Solution"` to `"Problem"` in its `sys.path.insert` line, or compare by running the Problem agent and using `tests/send_a2a_message.py`.
3. Run the full stack from the `Problem/` folders.

---

## Running from source

See the README's Quick start. In short, three terminals with the virtualenv active:

```powershell
cd Solution/movie_agent;    python .               # wait for "Uvicorn running on http://localhost:10005"
cd Solution/sushmita_agent; python .               # wait for "Uvicorn running on http://localhost:10006"
cd Solution/host_agent;     adk web --port 8000    # start last
```

Useful console output while developing:

- **Movie Agent**: the ClickHouse host at import, `Checking connectivity` / `ClickHouse connection successful` per tool call, and the full genre query and parameters (debug prints in `recommend_movies_by_genre`). `agent_executor.py` logs at DEBUG level.
- **Sushmita Agent**: the generated calendar at startup, CrewAI's verbose trace, `Final Result ===> ...`.
- **Host (`adk web` terminal)**: `agent_card: ...` for each remote agent, `agent_info: [...]`, and `send_response ...` for every A2A call. The UI's **Events** tab shows tool calls and responses.

---

## Testing

Test bottom-up; the first layer that fails is the broken one.

| Layer | Command | Needs | Pass |
|-------|---------|-------|------|
| ClickHouse + tools | `python tests/check_movie_tools.py` | ClickHouse only, no Gemini | `6/6 checks passed`, exit 0 |
| Card endpoints | `curl.exe http://localhost:10005/.well-known/agent-card.json` (and `:10006`) | servers running | JSON card |
| Movie Agent end to end | `python tests/send_a2a_message.py "Recommend 3 sci-fi movies"` | Movie Agent + Gemini | `State: TASK_STATE_COMPLETED` |
| Sushmita Agent end to end | `python tests/send_a2a_message.py "What genres do you like?" --url http://localhost:10006/` | Sushmita Agent + Gemini | `State: TASK_STATE_COMPLETED` |
| Everything | chat at http://localhost:8000 | all three | answers using both agents |

### `tests/check_movie_tools.py`

Loads the repo-root `.env`, imports `Solution/movie_agent/agent.py`, and calls each tool directly (list tools with `limit=5`). A check passes when the output contains an expected string and does not start with `Error`:

| Tool | Argument | Must contain |
|------|----------|--------------|
| `recommend_movies_by_genre` | `"Sci-Fi"` | `Star Wars` |
| `recommend_movies_by_actor` | `"Tom Hanks"` | `Forrest Gump` |
| `recommend_movies_by_director` | `"Steven Spielberg"` | `Schindler's List` |
| `recommend_movies_by_year` | `1999` | `Fight Club` |
| `search_movies_by_keyword` | `"matrix"` | `The Matrix` |
| `get_movie_details` | `"The Godfather"` | `Francis Ford Coppola` |

It prints each result with its timing (about 1.5 to 2.5 s per tool against the playground) and exits 1 on any failure. If you change the ranking (for example `MOVIE_MIN_CAST_SIZE`), an expected title may drop out of the top 5.

### `tests/send_a2a_message.py`

```text
usage: send_a2a_message.py [-h] [--url URL] text
```

Posts one raw JSON-RPC `SendMessage` request (no a2a-sdk client, just `httpx`, with header `A2A-Version: 1.0` and a 180 s timeout) to `--url` (default `http://localhost:10005/`, the Movie Agent). It prints `State: <state>`, the artifact text, then any status-message text. For a `TASK_STATE_FAILED` task that last part is the real error (for example a Gemini quota error). A JSON-RPC error (wrong method, bad params) is printed as `JSON-RPC error: {...}`. Exit code is 0 only for `TASK_STATE_COMPLETED`. The request format is documented in [A2A_PROTOCOL.md](A2A_PROTOCOL.md#json-rpc-on-the-wire).

There is no pytest suite; both scripts are plain Python and return meaningful exit codes, so they can be used in CI as-is (the second needs running agents and Gemini credit).

---

## Adding a tool to the Movie Agent

1. **Write the function** in `Solution/movie_agent/agent.py`, following the existing pattern:

   ```python
   def recommend_movies_by_decade(decade: int, limit: int = 10) -> str:
       """
       Recommends top-rated movies released in a decade.

       Args:
           decade: First year of the decade, e.g. 1990 (the dataset covers 1888-2008).
           limit: Maximum number of recommendations to return (default: 10).

       Returns:
           A string listing movies from that decade.
       """
       client = get_clickhouse_client()
       try:
           query = """
           SELECT m.name AS title, m.year AS year, round(toFloat64(m.rank), 1) AS rating
           FROM movies AS m
           INNER JOIN (
               SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
           ) AS c ON c.movie_id = m.id
           WHERE intDiv(m.year, 10) * 10 = {decade:UInt32}
             AND m.id NOT IN (SELECT movie_id FROM genres WHERE genre IN ('Short', 'Documentary', 'Adult'))
             AND m.rank > 0
             AND c.cast_size >= {min_cast:UInt32}
           ORDER BY m.rank DESC, c.cast_size DESC
           LIMIT {limit:UInt32}
           """
           result = client.query(query, parameters={
               "decade": int(decade), "min_cast": MIN_CAST_SIZE, "limit": int(limit)})
           movies = [f"- {to_display_title(r['title'])} ({r['year']}) [Rating: {r['rating'] or 'N/A'}]"
                     for r in result.named_results()]
           return f"Top movies of the {decade}s:\n" + "\n".join(movies) if movies \
               else f"No movies found for the {decade}s"
       except Exception as e:
           return f"Error querying movies: {str(e)}"
       finally:
           client.close()
   ```

   Rules that keep tools reliable:
   - **Type hints and a Google-style docstring** with `Args:`; ADK turns them into the function declaration the model sees.
   - **Return a string** (or a JSON-serialisable dict). Catch exceptions and return an error string rather than raising.
   - **Bind every value** with `{name:Type}`; use f-strings only for trusted constants like `PERSON_NAME`, and double the braces of placeholders inside f-strings.
   - Convert titles with `to_display_title()` on output and accept "The X" input with `to_imdb_title()` if you match titles.
   - Always `client.close()` in `finally`.
2. **Register it** in `create_agent()`: add it to `tools=[...]` and add a line to the instruction's *Core Directives* saying when to use it.
3. **Test it without the LLM**: add a `(agent.recommend_movies_by_decade, 1990, "<expected title>")` entry to `CHECKS` in `tests/check_movie_tools.py` (the script calls list tools as `tool(arg, limit=5)`).
4. Optionally add an example prompt to the `AgentSkill` in `__main__.py` so the card advertises it.
5. Restart the Movie Agent and try it with `python tests/send_a2a_message.py "Best movies of the 1990s"`.

---

## Adding a new remote agent to the Host

The Host discovers agents from their cards, so a new agent needs no Host code beyond its URL.

1. **Create the server**, for example `Solution/weather_agent/` with `agent.py`, `agent_executor.py` and `__main__.py`, copying the Sushmita Agent's structure:
   - an `AgentExecutor` subclass whose `execute()` enqueues the task if needed, calls `start_work()`, runs your logic (wrap blocking calls in `asyncio.to_thread`), then `add_artifact(...)` + `complete()`, or `failed(...)` on error;
   - an `AgentCard` with a unique `name`, a clear `description` (it goes into the Host's prompt), `supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0", url="http://localhost:<port>/")]`, skills and capabilities;
   - `DefaultRequestHandler(agent_executor=..., task_store=InMemoryTaskStore(), agent_card=...)`, `create_agent_card_routes` + `create_jsonrpc_routes(..., rpc_url="/")`, `uvicorn.run(...)` on a **free port** (for example 10007).
2. **Check it on its own**: `curl.exe http://localhost:10007/.well-known/agent-card.json` and `python tests/send_a2a_message.py "hello" --url http://localhost:10007/`.
3. **Register it with the Host**: add `"http://localhost:10007"` to `friend_agent_urls` in `_get_initialized_host_agent_sync()` (`Solution/host_agent/host/agent.py`).
4. **Teach the Host when to use it**: the `<Available Agents>` block will list it automatically, but the workflow in `root_instruction()` only mentions the Movie and Sushmita agents by name. Add a directive describing when to call the new agent and its exact card name.
5. Start the new agent before `adk web`, then restart `adk web` (cards are fetched only at import).

---

## Notebooks

Install Jupyter into the virtualenv (`pip install jupyterlab`) and run `jupyter lab Notebook/`. Both notebooks read the same `CLICKHOUSE_*` variables from `.env` (via `load_dotenv()`, which in a notebook searches upwards from the working directory) with the same defaults as the agent.

| Notebook | Sections |
|----------|----------|
| `clickhouse_connection.ipynb` | 1. Connection settings; 2. Install the driver (`%pip install clickhouse-connect python-dotenv pandas`); 3. Create the client and verify connectivity (`ping()`); 4. Sample query: row counts per table and the first 25 movies, with timing; 5. Parameterised query: top movies in a genre (`{genre:String}`, `{min_cast:UInt32}`); 6. Same query as a pandas DataFrame (`client.query_df`) |
| `clickhouse_db_exploration.ipynb` | Setup; Check datatypes (`SHOW TABLES`, row counts, `DESCRIBE`, `system.columns`); Top movies; Movies per genre; Movies released per decade; Most active actors; Most active directors; Director filmography; Actors and directors (`groupUniqArray`); Actor roles in a movie (*The Godfather*); Users who rated most movies (**not ported**: no user data); Rating distribution; Duplicate movie check |

The exploration notebook is a port of the earlier Neo4j/Cypher notebook; its markdown cells note the Cypher each query replaces.

---

## Upgrading dependencies

Current pins (`requirements.txt`):

```text
google-adk>=2.12.0,<3
a2a-sdk[http-server]>=1.2.3,<2
crewai[tools]>=1.15.28,<2
clickhouse-connect>=1.11.0
nest-asyncio>=1.6.0
python-dotenv, click, uvicorn, starlette, httpx, pydantic, pandas (unpinned)
```

Notes from the upgrade to these versions (a2a-sdk 0.2 -> 1.2, google-adk 1.x -> 2.12):

- **a2a-sdk major versions change the API substantially.** 1.x switched to protobuf types, renamed JSON-RPC methods (`SendMessage`), moved the card URL into `supported_interfaces`, replaced `A2AClient` with `ClientFactory` and `A2AStarletteApplication` with route factories, and requires the executor to enqueue the `Task` first. The full list is in [A2A_PROTOCOL.md](A2A_PROTOCOL.md#migrating-from-a2a-sdk-0x-to-1x); each change is marked in the code with an `a2a-sdk >= 1.0` comment. Keep the `<2` upper bound until you have checked a 2.x release against that list.
- **google-adk 2.x** reports model errors as events (`event.error_code` / `event.error_message`) instead of raising; the Movie executor relies on this to fail the task with the real error.
- **crewai 1.x** refuses synchronous `kickoff()` inside a running event loop (hence `asyncio.to_thread`) and needs Python 3.11 to 3.13 (not 3.14 at the time of writing).
- **Gemini models get retired.** `gemini-2.0-flash` was shut down on 2026-06-01, which is why the default is now `gemini-3.8-flash` everywhere. Change models with `GEMINI_MODEL` / `MOVIE_AGENT_MODEL`, not in code.
- After any upgrade, run the [testing ladder](#testing) from the bottom: `check_movie_tools.py`, the card URLs, `send_a2a_message.py` against both agents, then the web UI.
- Upgrade with the same tool you installed with, e.g. `uv pip install --python .venv -r requirements.txt --upgrade` or `pip install -U -r requirements.txt`.

---

## Windows notes

- **Path with a space.** The repo folder name `A2A- Protocol-with-ClickHouse` contains a space. Quote it in every shell command:

  ```powershell
  cd "C:\Users\<you>\Documents\GitHub\A2A- Protocol-with-ClickHouse"
  & ".\.venv\Scripts\python.exe" tests\check_movie_tools.py
  ```

  In PowerShell, a quoted executable path needs the call operator `&`.
- **Long paths.** crewai fails to import (a `ModuleNotFoundError` from inside `crewai`) when the virtualenv sits in a long, deeply nested path. Keep it at `<repo>\.venv`. Enabling Windows long-path support (`LongPathsEnabled`) can also help, but a short venv path is the reliable fix.
- **PowerShell execution policy.** If `.venv\Scripts\Activate.ps1` is blocked ("running scripts is disabled"), run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use `cmd` with `.venv\Scripts\activate.bat`, or skip activation and call `.venv\Scripts\python.exe` directly (`adk` is at `.venv\Scripts\adk.exe`).
- **`curl` vs `curl.exe`.** In Windows PowerShell 5.1, `curl` is an alias for `Invoke-WebRequest`. Use `curl.exe` for the real curl, or `Invoke-RestMethod http://localhost:10005/.well-known/agent-card.json`.
- **Sending JSON by hand.** Quoting JSON for `curl.exe -d` in PowerShell is error-prone; prefer `python tests/send_a2a_message.py "..."`.
- **Line endings.** `.gitattributes` sets `* text=auto`, so files are normalised to LF in the repository.
- **Stopping agents.** `Ctrl+C` in each terminal. If a port is still busy (`Address already in use`), an earlier process is still running in another terminal.
