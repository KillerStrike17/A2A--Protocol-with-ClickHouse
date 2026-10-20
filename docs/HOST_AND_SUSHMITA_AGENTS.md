# Host Agent and Sushmita Agent

- [Host Agent](#host-agent)
  - [How adk web finds it](#how-adk-web-finds-it)
  - [Initialisation](#initialisation)
  - [Dynamic instruction](#dynamic-instruction)
  - [The `send_message` tool](#the-send_message-tool)
  - [Movie plan tools](#movie-plan-tools)
- [Sushmita Agent](#sushmita-agent)
  - [Data: calendar, genres, watched movies](#data-calendar-genres-watched-movies)
  - [CrewAI tools](#crewai-tools)
  - [The crew](#the-crew)
  - [A2A executor](#a2a-executor)
  - [Why `invoke` runs in `asyncio.to_thread`](#why-invoke-runs-in-asyncioto_thread)

---

## Host Agent

Code: `Solution/host_agent/host/` (`agent.py`, `movie_tools.py`, `remote_agent_connection.py`, `__init__.py`). Framework: Google ADK 2.12. It is not an A2A server; it is run by the ADK developer web UI and acts as an A2A **client** of the other two agents.

### How adk web finds it

```powershell
cd Solution/host_agent
adk web --port 8000
```

`adk web` treats each sub-folder of its working directory as an agent app. `Solution/host_agent/host/__init__.py` contains:

```python
from .agent import root_agent
```

ADK looks for a module-level variable called **`root_agent`** in the package, so the app appears as **host** in the UI dropdown. If you run `adk web` from anywhere else (for example the repo root), `host` is not listed. The same applies to `Problem/host_agent`.

### Initialisation

At the bottom of `agent.py`:

```python
root_agent = _get_initialized_host_agent_sync()
```

`_get_initialized_host_agent_sync()` runs `HostAgent.create(remote_agent_addresses=friend_agent_urls)` with `asyncio.run()`:

- `friend_agent_urls` is hard-coded: `http://localhost:10005` (Movie Agent) and `http://localhost:10006` (Sushmita Agent).
- `nest_asyncio.apply()` (called at import) lets `asyncio.run()` work even though `adk web` already has an event loop running. If it still fails with "cannot be called from a running event loop", a warning is printed and `root_agent` ends up `None`.
- `HostAgent.__init__` creates empty dicts, an ADK `Agent` and its own `Runner`; `_async_init_components()` then fetches every agent card with `A2ACardResolver`, wraps each in a `RemoteAgentConnections`, and stores them under **`card.name`**. Failures are printed and skipped.
- Finally `hosting_agent_instance.create_agent()` builds a fresh `Agent` whose tools are bound methods of the initialised instance, so `send_message` can see the connections. That `Agent` is `root_agent`.

The agent definition:

```python
Agent(
    model=GEMINI_MODEL,                       # env GEMINI_MODEL or "gemini-3.8-flash"
    name="Host_Agent",
    instruction=self.root_instruction,        # a callable, see below
    description=(
        "This Host agent plans a movie night with friends: it asks the Movie Agent for "
        "recommendations and the Sushmita Agent for availability and preferences."
    ),
    tools=[self.send_message, confirm_movie_plan, get_movie_plans, cancel_movie_plan],
)
```

`HostAgent.stream()` and the instance's own `Runner` are not used by `adk web`; they would let you drive the Host from your own code.

### Dynamic instruction

`instruction` is the method `root_instruction(self, context: ReadonlyContext) -> str`. ADK calls it on every model request, so two parts are filled in at run time:

- `**Today's Date (YYYY-MM-DD):** {datetime.now().strftime("%Y-%m-%d")}`, needed to turn "this weekend" into dates for Sushmita and for `confirm_movie_plan`.
- `<Available Agents> {self.agents} </Available Agents>`, one JSON line per fetched card: `{"name": "...", "description": "..."}`, or `No friends found` when no card was fetched.

The rest is a fixed workflow:

1. Ask the **Sushmita Agent** for favourite genres and watched movies; ask about availability **only** when the user explicitly asks to check availability or schedule.
2. Ask the **Movie Agent** for recommendations in those genres.
3. Remove movies Sushmita has already watched.
4. Present the filtered list (and matching time slots if availability was checked).
5. When the user picks, call `confirm_movie_plan` (date `YYYY-MM-DD`, time `HH:MM`); use `get_movie_plans` / `cancel_movie_plan` on request.

It also tells the model to pass the exact agent names (`"Sushmita Agent"`, `"Movie Agent"`), not to ask permission before contacting agents, and to rely on tools rather than assumptions.

### The `send_message` tool

```python
async def send_message(self, agent_name: str, task: str, tool_context: ToolContext)
```

ADK exposes `agent_name` and `task` to the model (it injects `tool_context` itself). The docstring "Sends a task to a remote friend agent." is the tool description.

Steps:

1. Look up `self.remote_agent_connections[agent_name]`; unknown names raise `ValueError(f"Agent {agent_name} not found")`.
2. Read this agent's conversation from `tool_context.state`: `a2a_context_id:<agent name>` (a new `uuid4()` on the first call) and `a2a_task_id:<agent name>`. A `task_id` is only added to the message if one is stored, because a2a-sdk 1.x servers answer unknown task ids with `TaskNotFoundError` (see [ARCHITECTURE.md](ARCHITECTURE.md#context_id-and-task_id)).
3. Build `Message(role=Role.ROLE_USER, parts=[Part(text=task)], message_id=..., context_id=...)` and `SendMessageRequest(message=message)`.
4. `await client.send_message(message_request)`: `RemoteAgentConnections` uses an a2a-sdk client created by `ClientFactory(ClientConfig(streaming=False, httpx_client=httpx.AsyncClient(timeout=30)))`, iterates the response stream and returns the last `StreamResponse`.
5. Print the raw response (handy for debugging in the `adk web` terminal). No response returns `[{"text": "No response from <agent>."}]`; a bare `message` reply (no task) returns that message's parts.
6. Save the conversation for next time: store the task's `context_id`, and store its `id` as the task id only if the task is `TASK_STATE_INPUT_REQUIRED` (otherwise clear it).
7. Convert the task with `MessageToDict` and return all artifact parts, e.g. `[{"text": "..."}]`.
8. If the task did not complete, append `<agent> did not complete the task (<STATE>).` (or `<agent> needs more input:`) and the task's status-message parts, so the model sees the real error or question instead of an empty list.

### Movie plan tools

`movie_tools.py` holds a module-level, in-memory dict:

```python
MOVIE_PLANS: Dict[str, Dict[str, str]] = {}
```

It lives as long as the `adk web` process and is shared by all chat sessions. `_initialize_movie_plans()` is an empty placeholder that runs at import.

| Tool | Arguments | Behaviour | Returns |
|------|-----------|-----------|---------|
| `confirm_movie_plan` | `movie_title`, `date` (`YYYY-MM-DD`), `time` (`HH:MM`), `participants="Sushmita"` | Validates date/time with `strptime` and that the title is non-empty; stores `{id, movie_title, date, time, participants, created_at}` under a new `uuid4` | `{"status": "success", "message": ..., "plan_id": ..., "plan": {...}}` or `{"status": "error", "message": ...}` |
| `get_movie_plans` | `date_filter: Optional[str] = None` | With a filter: validates it and returns plans on that date. Without: all plans sorted by `(date, time)` | `{"status": "success", "message": "Found N movie plan(s)...", "plans": [...]}` |
| `cancel_movie_plan` | `plan_id` | Removes the plan if it exists | success message with title/date/time, or an error |

The tools do not check Sushmita's calendar; the instruction makes the model do that through the Sushmita Agent before confirming.

---

## Sushmita Agent

Code: `Solution/sushmita_agent/` (`agent.py`, `agent_executor.py`, `__main__.py`). Framework: CrewAI 1.15. It is an A2A server on `localhost:10006` with two skills, `availability_checker` and `movie_preferences`.

### Data: calendar, genres, watched movies

All data is hard-coded or generated in `agent.py`:

- **Calendar**: `MY_CALENDAR = generate_calendar()` runs at import. For each of the next 7 days (starting today) it picks 8 random hourly slots out of `08:00` to `20:00` and sorts them. It is printed to the console at startup and changes on every restart.
- **Favourite genres**: `MOVIE_GENRE_PREFERENCES = ["Action", "Comedy", "Drama", "Sci-Fi", "Thriller", "Romance"]`. These are spelled like the IMDB genre names, so the Host can pass them straight to the Movie Agent.
- **Watched movies**: `WATCHED_MOVIES`, 10 titles: The Matrix, Inception, Interstellar, The Dark Knight, Pulp Fiction, Forrest Gump, The Shawshank Redemption, Titanic, Avatar, The Avengers. (Several are after 2008 and therefore not in the IMDB dataset at all.)

### CrewAI tools

Both are `crewai.tools.BaseTool` subclasses with a pydantic `args_schema`. The `name` and `description` are what the CrewAI LLM sees.

| Class | `name` | Input | Behaviour |
|-------|--------|-------|-----------|
| `AvailabilityTool` | `Calendar Availability Checker` | `date_range: str`, e.g. `2024-07-28` or `2024-07-28 to 2024-07-30` | Splits on `"to"`, parses both ends as `%Y-%m-%d`, and for each day returns `On <date>, I am available at: ...` or `I am not available on <date>.` Days outside the generated 7-day window count as unavailable. Bad input returns a message asking for `YYYY-MM-DD`; start after end returns an error message. |
| `MoviePreferencesTool` | `Movie Preferences Manager` | `query: str` | Keyword routing on the lower-cased query: contains `genre` / `preference` / `like` -> genres only; else contains `watched` / `seen` / `already` -> watched list only; otherwise both. |

Because the routing checks genres first, a query like "what do you like and what have you watched?" returns only the genres; the LLM typically calls the tool again for the watched list.

### The crew

`SchedulingAgent.__init__`:

- Requires `GOOGLE_API_KEY` (raises `ValueError` otherwise) and creates `LLM(model=CREWAI_GEMINI_MODEL, api_key=...)`. `CREWAI_GEMINI_MODEL` is `GEMINI_MODEL` (default `gemini-3.8-flash`) with a `gemini/` prefix added if missing; CrewAI uses that prefix to route to Google's Gemini API.
- Creates one CrewAI `Agent` with role "Personal Assistant for Scheduling and Movie Preferences", a goal and backstory describing the two responsibilities, `allow_delegation=False`, `verbose=True`, and both tools.

`SchedulingAgent.invoke(question)` builds a new `Task` per call whose description embeds the question, **today's date**, and which tool to use for which kind of question, plus an `expected_output`. It runs a one-agent, one-task `Crew(process=Process.sequential)` with `crew.kickoff()` and returns `str(result)`. `verbose=True` makes CrewAI print its reasoning and tool calls in the agent's terminal.

### A2A executor

`SchedulingAgentExecutor` (`agent_executor.py`):

1. Validates that the context has a task id, context id and message.
2. Enqueues a new `Task` (`new_task_from_user_message`) if there is no current task, then `TaskUpdater.start_work()`.
3. `_validate_request()` always returns `False` (a hook; if it returned `True` the executor would raise `InvalidParamsError`).
4. `query = context.get_user_input()` (the text of the message parts).
5. `result = await asyncio.to_thread(self.agent.invoke, query)`.
6. On exception: `updater.failed(message=... "Agent error: <e>")` and return.
7. Otherwise `updater.add_artifact([new_text_part(result)])` and `updater.complete()`.

`cancel()` raises `UnsupportedOperationError`. The card sets `capabilities.streaming=False`; the agent produces a single final answer.

### Why `invoke` runs in `asyncio.to_thread`

The a2a-sdk server is asynchronous: `execute()` runs on uvicorn's event loop. `crew.kickoff()` is synchronous and, in crewai 1.x, refuses to run when it detects a running event loop in the current thread. Calling it directly from `execute()` would therefore fail (and, even if it ran, would block the event loop for the whole LLM conversation, stalling every other request to the server).

`asyncio.to_thread(self.agent.invoke, query)` runs the blocking crew in a worker thread that has no event loop, and lets `execute()` await the result without blocking the server. Exceptions raised in the thread propagate through the `await`, which is how Gemini errors end up in the `TASK_STATE_FAILED` message.
