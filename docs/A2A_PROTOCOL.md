# The A2A protocol in this project

[A2A (Agent2Agent)](https://a2a-protocol.org) is an open protocol that lets agents built with different frameworks call each other over HTTP. Here it connects an ADK Host Agent to an ADK Movie Agent and a CrewAI Sushmita Agent. This project uses **a2a-sdk 1.2.3** (`a2a-sdk[http-server]>=1.2.3,<2` in `requirements.txt`), which implements protocol version **1.0**.

- [Concepts at a glance](#concepts-at-a-glance)
- [Agent cards](#agent-cards)
- [Skills](#skills)
- [Messages and parts](#messages-and-parts)
- [Tasks and task states](#tasks-and-task-states)
- [Artifacts](#artifacts)
- [JSON-RPC on the wire](#json-rpc-on-the-wire)
- [Server side: how an agent is exposed](#server-side-how-an-agent-is-exposed)
- [Client side: how the Host calls an agent](#client-side-how-the-host-calls-an-agent)
- [Migrating from a2a-sdk 0.x to 1.x](#migrating-from-a2a-sdk-0x-to-1x)

---

## Concepts at a glance

| Concept | What it is | In this project |
|---------|-----------|-----------------|
| **Agent card** | JSON self-description of an agent, served at a well-known URL | `GET /.well-known/agent-card.json` on ports 10005 and 10006 |
| **Skill** | A capability listed on the card, with examples | `movie_recommendations`; `availability_checker`, `movie_preferences` |
| **Message** | One turn from a user or agent, made of parts | The Host sends `role=ROLE_USER` messages with one text part |
| **Part** | A piece of content: text, raw bytes, URL or structured data | Only text parts are exchanged |
| **Task** | A unit of work with an id, a context id, a status and artifacts | One task per `SendMessage` call |
| **Artifact** | An output produced by a task | The agent's final answer text |
| **context_id** | Groups messages and tasks into one conversation | The Movie Agent uses it as its ADK session id |

All a2a-sdk 1.x types (`AgentCard`, `Message`, `Part`, `Task`, ...) are **protobuf messages** generated from the A2A `.proto` definition (`a2a.types` re-exports `a2a_pb2`). In Python you use snake_case field names (`message_id`, `context_id`); on the wire they are serialised with protobuf's JSON mapping, i.e. camelCase (`messageId`, `contextId`) and enum value names as strings (`"ROLE_USER"`, `"TASK_STATE_COMPLETED"`).

---

## Agent cards

A card tells a client who the agent is, how to reach it and what it can do. The Sushmita Agent builds its card in `Solution/sushmita_agent/__main__.py`:

```python
agent_card = AgentCard(
    name="Sushmita Agent",
    description="A friendly agent that helps with scheduling availability and provides ...",
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            protocol_version="1.0",
            url=agent_host_url,          # HOST_OVERRIDE or http://localhost:10006/
        )
    ],
    version="1.0.0",
    default_input_modes=SchedulingAgent.SUPPORTED_CONTENT_TYPES,   # ["text/plain"]
    default_output_modes=SchedulingAgent.SUPPORTED_CONTENT_TYPES,
    capabilities=AgentCapabilities(streaming=False),
    skills=[skill1, skill2],
)
```

Served as JSON it looks like this (descriptions shortened, one skill shown):

```json
{
  "name": "Sushmita Agent",
  "description": "A friendly agent that helps with scheduling availability ...",
  "supportedInterfaces": [
    {
      "url": "http://localhost:10006/",
      "protocolBinding": "JSONRPC",
      "protocolVersion": "1.0"
    }
  ],
  "version": "1.0.0",
  "capabilities": { "streaming": false },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    {
      "id": "movie_preferences",
      "name": "Movie Preferences",
      "description": "Get my favorite movie genres and list of movies I've already watched.",
      "tags": ["movies", "preferences", "genres"],
      "examples": ["What genres of movies do you like?", "..."]
    }
  ]
}
```

Things to note:

- **`supported_interfaces`** replaces the single top-level `url` of earlier versions. Each entry names a transport (`protocol_binding`: here `JSONRPC`), a protocol version and the endpoint URL. The client picks a transport it supports from this list, so **the URL in the card is the URL the Host will POST to**. That is why `HOST_OVERRIDE` matters if the Sushmita Agent sits behind a proxy or tunnel.
- **`name`** is used as the key on the Host side: the Host's LLM must call `send_message(agent_name="Movie Agent" | "Sushmita Agent", ...)`, exactly as in the card.
- **`description`** is injected into the Host's prompt (`<Available Agents>` block), so it is effectively prompt text.
- **`capabilities.streaming`**: `True` for the Movie Agent, `False` for Sushmita. The Host does not use streaming either way (see below).

Check a running agent's card:

```powershell
curl.exe http://localhost:10005/.well-known/agent-card.json   # Windows PowerShell: use curl.exe, not curl
```

---

## Skills

Skills are descriptive metadata: A2A does not route requests by skill id. Every message goes to the same JSON-RPC endpoint, and the agent's own LLM decides what to do. Skills (with their `examples`) help humans and orchestrating LLMs understand what to ask.

| Agent | Skill id | Name | Example |
|-------|----------|------|---------|
| Movie Agent | `movie_recommendations` | Movie Recommendations | "What movies has Tom Hanks acted in?" |
| Sushmita Agent | `availability_checker` | Availability Checker | "When are you available this week?" |
| Sushmita Agent | `movie_preferences` | Movie Preferences | "What genres of movies do you like?" |

In this project only the card's `name` and `description` reach the Host's prompt; the skills are not.

---

## Messages and parts

A `Message` has a `role` (`ROLE_USER` or `ROLE_AGENT`), a client-generated `message_id`, a list of `parts`, and optional `context_id` / `task_id`.

In a2a-sdk 1.x a `Part` is a single flat protobuf message with a `content` oneof:

| oneof field | Meaning | Converted by the Movie Agent to/from |
|-------------|---------|--------------------------------------|
| `text` | Plain text | `types.Part(text=...)` |
| `raw` | Bytes (+ `media_type`) | `types.Part(inline_data=types.Blob(...))` |
| `url` | Link to a file (+ `media_type`) | `types.Part(file_data=types.FileData(...))` |
| `data` | Structured JSON-like data | not supported (raises `ValueError`) |

The converters live in `Solution/movie_agent/agent_executor.py` (`convert_a2a_part_to_genai`, `convert_genai_part_to_a2a`). They use `part.WhichOneof("content")` to find which field is set. The Sushmita Agent only handles text: it reads the request with `context.get_user_input()` and answers with `new_text_part(result)` from `a2a.helpers`.

How the Host builds a message (`HostAgent.send_message`):

```python
message = Message(
    role=Role.ROLE_USER,
    parts=[Part(text=task)],
    message_id=str(uuid.uuid4()),
    context_id=context_id,
)
if task_id:                # only when continuing a task the server already knows
    message.task_id = task_id
message_request = SendMessageRequest(message=message)
```

---

## Tasks and task states

The server wraps every request in a `Task` (`id`, `context_id`, `status`, `artifacts`, `history`). `status` holds a `state`, an optional `message` and a timestamp.

| State | Used here |
|-------|-----------|
| `TASK_STATE_SUBMITTED` | Initial state of a newly created task |
| `TASK_STATE_WORKING` | Set by `TaskUpdater.start_work()`; the Movie Agent also sends WORKING updates carrying intermediate model text |
| `TASK_STATE_COMPLETED` | Set by `TaskUpdater.complete()` after the answer artifact is added |
| `TASK_STATE_FAILED` | Set by `TaskUpdater.failed(message=...)`; the message holds the real error |
| `TASK_STATE_CANCELED`, `TASK_STATE_REJECTED`, `TASK_STATE_INPUT_REQUIRED`, `TASK_STATE_AUTH_REQUIRED` | Defined by the protocol, not produced by these agents (cancel raises `UnsupportedOperationError`) |

The executor lifecycle used by both agents:

```python
async def execute(self, context: RequestContext, event_queue: EventQueue):
    # 1. a2a-sdk >= 1.0: the Task must be on the queue before any status/artifact events
    if not context.current_task:
        await event_queue.enqueue_event(new_task_from_user_message(context.message))
    updater = TaskUpdater(event_queue, context.task_id, context.context_id)
    await updater.start_work()                       # 2. WORKING
    ...                                              # 3. run ADK / CrewAI
    await updater.add_artifact(parts)                # 4. answer as an artifact
    await updater.complete()                         # 5. COMPLETED
    # on error instead: await updater.failed(message=updater.new_agent_message([Part(text=...)]))
```

### Task ids are server-owned

With a2a-sdk 1.x the server creates task ids. If a client sends a message whose `task_id` the server does not have, `DefaultRequestHandler` raises **`TaskNotFoundError`** (JSON-RPC error code `-32001`, message `Task <id> was specified but does not exist`). A message aimed at a task that has already reached a terminal state is rejected with `UnsupportedOperationError`. This is why the Host only sets `message.task_id` when a task id is already stored in its tool state; otherwise it sends just a `context_id` and lets the server create the task.

---

## Artifacts

An `Artifact` (`artifact_id`, optional `name` / `description`, `parts`) is the output of a task. Both agents put their final answer into one artifact via `TaskUpdater.add_artifact(parts)`. The Host reads only the artifacts:

```python
json_content = MessageToDict(send_response.task)     # protobuf -> dict (camelCase keys)
resp = []
for artifact in json_content.get("artifacts", []):
    resp.extend(artifact.get("parts", []))            # e.g. [{"text": "Here are 5 Sci-Fi movie ..."}]
return resp
```

---

## JSON-RPC on the wire

Both agents expose a single JSON-RPC 2.0 endpoint at `POST /`. a2a-sdk 1.x method names are PascalCase: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`, the push-notification config methods and `GetExtendedAgentCard`. This project uses `SendMessage`.

### Request

This is exactly what `tests/send_a2a_message.py` sends:

```http
POST http://localhost:10005/
Content-Type: application/json
A2A-Version: 1.0
```

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "messageId": "6f0c1e7e-2b0b-4d1e-9a51-3c1f0d3f5a10",
      "parts": [{ "text": "Recommend 3 sci-fi movies" }]
    }
  }
}
```

The Host's a2a-sdk client sends the same shape plus a `contextId` in the message (and `taskId` when continuing a task); `ClientFactory` adds the `A2A-Version: 1.0` header automatically.

### Successful response

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "task": {
      "id": "<server-generated task id>",
      "contextId": "<context id>",
      "status": { "state": "TASK_STATE_COMPLETED", "timestamp": "..." },
      "artifacts": [
        {
          "artifactId": "<id>",
          "parts": [{ "text": "Here are 3 Sci-Fi movie recommendations:\n- ..." }]
        }
      ],
      "history": [
        { "messageId": "...", "role": "ROLE_USER", "parts": [{ "text": "Recommend 3 sci-fi movies" }] }
      ]
    }
  }
}
```

`result` is a `SendMessageResponse`, a oneof of `task` or `message`. These agents always return a `task`, and both the test script (`body["result"]["task"]`) and the Host (`send_response.HasField("task")`) rely on that.

### Failed task

A failure inside the agent is **not** a JSON-RPC error: the call succeeds and the task carries the failure.

```json
{
  "result": {
    "task": {
      "status": {
        "state": "TASK_STATE_FAILED",
        "message": {
          "role": "ROLE_AGENT",
          "parts": [{ "text": "Agent error <code>: <error message from Gemini>" }]
        }
      }
    }
  }
}
```

The Movie Agent formats the text as `Agent error <code>: <message>`, the Sushmita Agent as `Agent error: <exception>`.

### Protocol error

Wrong method names, invalid params or unknown task ids return a JSON-RPC `error` object instead of `result`; `tests/send_a2a_message.py` prints it as `JSON-RPC error: {...}`.

---

## Server side: how an agent is exposed

From `Solution/movie_agent/__main__.py` (the Sushmita Agent is identical apart from the executor and card):

```python
request_handler = DefaultRequestHandler(
    agent_executor=agent_executor,       # your AgentExecutor subclass
    task_store=InMemoryTaskStore(),
    agent_card=agent_card,               # new in 1.x
)
routes = []
routes.extend(create_agent_card_routes(agent_card))                 # GET /.well-known/agent-card.json
routes.extend(create_jsonrpc_routes(request_handler, rpc_url="/"))  # POST /
server = Starlette(routes=routes)
uvicorn.run(server, host=host, port=port)
```

`AgentExecutor` is the only class you implement: `execute(context, event_queue)` and `cancel(context, event_queue)`. See [ARCHITECTURE.md](ARCHITECTURE.md#inside-each-remote-agent-server).

---

## Client side: how the Host calls an agent

1. **Discover**: `A2ACardResolver(httpx_client, "http://localhost:10005").get_agent_card()` downloads and parses the card (`HostAgent._async_init_components`).
2. **Create a client** (`RemoteAgentConnections.__init__`):

   ```python
   self.agent_client = ClientFactory(
       ClientConfig(streaming=False, httpx_client=self._httpx_client)
   ).create(agent_card)
   ```

   The factory chooses a transport from `agent_card.supported_interfaces` (JSON-RPC here) and uses the URL found there.
3. **Send**: `client.send_message(request)` is an **async iterator** of `StreamResponse` items in a2a-sdk 1.x, even when streaming is off. `RemoteAgentConnections.send_message` iterates it and returns the last item, which is the final `Task`:

   ```python
   last_response = None
   async for response in self.agent_client.send_message(message_request):
       last_response = response
   return last_response
   ```

---

## Migrating from a2a-sdk 0.x to 1.x

The project was originally written for a2a-sdk 0.2 / google-adk 1.x. The upgrade touched every file that speaks A2A; the code comments marked `a2a-sdk >= 1.0` point at each change.

| Area | a2a-sdk 0.x (old code) | a2a-sdk 1.x (this code) | Where |
|------|------------------------|-------------------------|-------|
| Type system | pydantic models | protobuf messages (`a2a.types` -> `a2a_pb2`) | everywhere |
| Field names in Python | camelCase (`messageId`, `contextId`) | snake_case (`message_id`, `context_id`); camelCase only in JSON | `HostAgent.send_message` |
| Roles | string / lower-case enum (`"user"`) | `Role.ROLE_USER`, `Role.ROLE_AGENT`; JSON `"ROLE_USER"` | `HostAgent.send_message`, `tests/send_a2a_message.py` |
| Parts | `Part(root=TextPart(text=...))`, `FilePart`, ... | flat `Part(text=...)` / `Part(raw=...)` / `Part(url=...)` with a `content` oneof | both executors, Host |
| Task states | short names | `TaskState.TASK_STATE_WORKING`, `TASK_STATE_COMPLETED`, ... | executors, test script |
| JSON-RPC method | `message/send` | `SendMessage` | test script; servers return `Method not found` for the old name |
| Card endpoint URL | top-level `url` on `AgentCard` | `supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0", url=...)]` | both `__main__.py` |
| Server app | `A2AStarletteApplication(agent_card=..., http_handler=...)` | `Starlette(routes=create_agent_card_routes(...) + create_jsonrpc_routes(...))` | both `__main__.py` |
| Request handler | `DefaultRequestHandler(agent_executor, task_store)` | also takes `agent_card=` | both `__main__.py` |
| Client | `A2AClient(httpx_client, agent_card)` (legacy, removed) | `ClientFactory(ClientConfig(...)).create(agent_card)` | `remote_agent_connection.py` |
| `send_message` result | a single response object | async iterator of `StreamResponse`; take the last one and check `HasField("task")` | `remote_agent_connection.py`, Host |
| Response to dict | pydantic `.model_dump()` | `google.protobuf.json_format.MessageToDict(task)` | `HostAgent.send_message` |
| New task | `updater.submit()` | enqueue `new_task_from_user_message(context.message)` **before** any status/artifact event | both executors |
| Raising protocol errors | `raise ServerError(error=UnsupportedOperationError())` | `raise UnsupportedOperationError()` / `InvalidParamsError()` directly | both executors |
| Client-chosen task ids | tolerated | rejected with `TaskNotFoundError` | `HostAgent.send_message` only sends a known `task_id` |
| Helpers | `a2a.utils` (`new_agent_text_message`, ...) | `a2a.helpers` (`new_task_from_user_message`, `new_text_part`) | executors |

The google-adk 1.x to 2.x upgrade had one visible effect on this code: model errors (quota, auth, ...) arrive as **events with `error_code` / `error_message`** instead of exceptions, so `MovieAgentExecutor._process_request` checks every event and fails the task explicitly. CrewAI 1.x refuses a synchronous `kickoff()` inside a running event loop, hence `asyncio.to_thread` in the Sushmita executor (see [HOST_AND_SUSHMITA_AGENTS.md](HOST_AND_SUSHMITA_AGENTS.md#why-invoke-runs-in-asyncioto_thread)).
