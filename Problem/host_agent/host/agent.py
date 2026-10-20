"""Host Agent: the Google ADK agent the user chats with in ``adk web``.

Role in the 3-agent system
    The Host Agent does not answer from its own knowledge. It delegates to two remote agents over the
    A2A protocol and combines their answers:

    - Movie Agent (Google ADK + ClickHouse IMDB data) at ``http://localhost:10005``
    - Sushmita Agent (CrewAI: availability, favourite genres, watched movies) at ``http://localhost:10006``

    It then saves, lists or cancels the chosen plan with the local tools in ``movie_tools.py``.

How it is run
    ``cd Solution/host_agent`` then ``adk web --port 8000``, and pick "host" in the web UI. ``adk web``
    imports the ``host`` package, whose ``__init__`` re-exports ``root_agent`` from this module.

Start-up flow (runs when this module is imported; see the bottom of the file)
    1. ``_get_initialized_host_agent_sync()`` runs ``HostAgent.create()`` with ``asyncio.run``.
    2. ``_async_init_components`` downloads each remote agent's card with ``A2ACardResolver`` (from the
       agent's well-known agent-card URL) and wraps it in a ``RemoteAgentConnections``.
    3. The name and description of each agent that answered are stored in ``self.agents``; the dynamic
       instruction (``root_instruction``) lists them under ``<Available Agents>``.
    4. ``create_agent()`` returns the ADK ``Agent``, which becomes the module-level ``root_agent``.

    Start the Movie and Sushmita agents first. If one cannot be reached, the error is only printed
    and that agent is left out of the instruction's agent list.

Environment variables (``load_dotenv()`` loads them from the nearest ``.env`` file)
    ``GEMINI_MODEL``: Gemini model for the host (default ``gemini-3.8-flash``).
    ``GOOGLE_API_KEY`` / ``GOOGLE_GENAI_USE_VERTEXAI``: read by the google-genai client inside ADK, not by
        this module directly.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Any, AsyncIterable, List

import httpx
import nest_asyncio
from a2a.client import A2ACardResolver
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskState,
)
from dotenv import load_dotenv
from google.adk import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from google.protobuf.json_format import MessageToDict

from .movie_tools import (
    confirm_movie_plan,
    get_movie_plans,
    cancel_movie_plan,
)
from .remote_agent_connection import RemoteAgentConnections

load_dotenv()
# nest_asyncio patches asyncio so asyncio.run() / run_until_complete() also work while an event loop is
# already running. `adk web` (a uvicorn server) or Jupyter may already be running a loop when this
# module is imported. Without the patch, the asyncio.run() call at import time (bottom of this file)
# would raise "asyncio.run() cannot be called from a running event loop".
nest_asyncio.apply()

# gemini-2.0-flash has been shut down; override with the GEMINI_MODEL env var.
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"


class HostAgent:
    """The Host agent.

    Holds the A2A connections to the remote agents and builds the ADK ``Agent`` that uses them. Create it
    with ``await HostAgent.create(urls)``, not ``HostAgent()``, so the remote agent cards get fetched.

    Attributes:
        remote_agent_connections: Agent-card name -> ``RemoteAgentConnections``; used by ``send_message``.
        cards: Agent-card name -> ``AgentCard``.
        agents: One JSON line (name + description) per remote agent, inserted into the instruction.
    """

    def __init__(
        self,
    ):
        self.remote_agent_connections: dict[str, RemoteAgentConnections] = {}
        self.cards: dict[str, AgentCard] = {}
        self.agents: str = ""
        # This first Agent is built before any cards are loaded. That works because the instruction is a
        # callable that reads self.agents when it runs. The module-level root_agent comes from a second
        # create_agent() call made after the cards were fetched.
        self._agent = self.create_agent()
        self._user_id = "host_agent"
        # This Runner (used by stream() below) lets code drive the HostAgent directly. `adk web` does not
        # use it: it wraps root_agent in its own runner and session services.
        self._runner = Runner(
            app_name=self._agent.name,
            agent=self._agent,
            artifact_service=InMemoryArtifactService(),
            session_service=InMemorySessionService(),
            memory_service=InMemoryMemoryService(),
        )

    async def _async_init_components(self, remote_agent_addresses: List[str]):
        """Downloads each remote agent's card and opens an A2A connection to it.

        Errors are printed and skipped, so the host still starts when one agent is down.

        Args:
            remote_agent_addresses: Base URLs of the remote A2A agents.
        """
        # This short-lived client only downloads the cards; each RemoteAgentConnections opens its own client.
        async with httpx.AsyncClient(timeout=30) as client:
            for address in remote_agent_addresses:
                card_resolver = A2ACardResolver(client, address)
                try:
                    card = await card_resolver.get_agent_card()
                    remote_connection = RemoteAgentConnections(
                        agent_card=card, agent_url=address
                    )
                    # Keyed by the card's `name` (e.g. "Sushmita Agent"); the LLM passes this exact name
                    # as `agent_name` to send_message.
                    self.remote_agent_connections[card.name] = remote_connection
                    self.cards[card.name] = card
                except httpx.ConnectError as e:
                    print(f"ERROR: Failed to get agent card from {address}: {e}")
                except Exception as e:
                    print(f"ERROR: Failed to initialize connection for {address}: {e}")

        # One JSON object per line; this text is inserted under <Available Agents> in the instruction.
        agent_info = [
            json.dumps({"name": card.name, "description": card.description})
            for card in self.cards.values()
        ]
        print("agent_info:", agent_info)
        self.agents = "\n".join(agent_info) if agent_info else "No friends found"

    @classmethod
    async def create(
        cls,
        remote_agent_addresses: List[str],
    ):
        """Async factory: creates the HostAgent and loads the remote agent cards.

        Args:
            remote_agent_addresses: Base URLs of the remote A2A agents.

        Returns:
            The initialised ``HostAgent``.
        """
        instance = cls()
        await instance._async_init_components(remote_agent_addresses)
        return instance

    def create_agent(self) -> Agent:
        """Builds the ADK ``Agent`` with its dynamic instruction and tools.

        ADK turns each function in ``tools`` into a function tool: the signature becomes the parameter
        schema and the docstring becomes the description the LLM sees. ``self.send_message`` is a bound
        method, so that tool can reach this instance's remote connections.
        """
        return Agent(
            model=GEMINI_MODEL,
            name="Host_Agent",
            # A callable instruction (InstructionProvider): ADK calls it with a ReadonlyContext before each
            # model request, so today's date and the agent list are always current.
            instruction=self.root_instruction,
            description=(
                "This Host agent plans a movie night with friends: it asks the Movie Agent for "
                "recommendations and the Sushmita Agent for availability and preferences."
            ),
            tools=[
                self.send_message,
                confirm_movie_plan,
                get_movie_plans,
                cancel_movie_plan,
            ],
        )

    def root_instruction(self, context: ReadonlyContext) -> str:
        """Returns the system instruction (used as an ADK InstructionProvider).

        ADK calls this with a ``ReadonlyContext`` (a read-only view of the session) before each LLM
        request. The context is not used here. The prompt includes today's date and ``self.agents``, the
        remote agents found at start-up, under ``<Available Agents>``.
        """
        return f"""
        **Role:** You are the Host Agent, an expert coordinator for movie recommendations and scheduling. Your primary function is to coordinate with the Movie Agent and Sushmita Agent to find a suitable movie and time to watch together.

        **Core Directives:**

        *   **Initiate Planning:** When asked to recommend a movie or plan a movie watching session, follow this workflow:
            1. First, ask the "Sushmita Agent" for:
               - Their favorite movie genres
               - The list of movies they've already watched
               - Their availability (ONLY if the user explicitly asks to check availability, schedule, or find a time)
            2. Then, ask the "Movie Agent" for movie recommendations based on Sushmita's genre preferences
            3. Filter out any movies from the recommendations that Sushmita has already watched
            4. Present the filtered movie recommendations
            5. If availability was checked, also present available time slots and match them with recommendations
            6. Help the user select a movie (and time if availability was checked), then confirm the plan

        *   **Task Delegation:** Use the `send_message` tool to communicate with agents.
            *   For Sushmita Agent:
              - ALWAYS ask: "What genres of movies do you like and what movies have you already watched?"
              - ONLY ask about availability if the user explicitly mentions checking availability, scheduling, or finding a time (e.g., "check her availability", "when is she free", "schedule a movie")
              - If availability is needed, ask: "Are you available to watch a movie this week? What are your favorite genres and what movies have you already watched?"
              - If availability is NOT needed, just ask: "What genres of movies do you like and what movies have you already watched?"
            *   For Movie Agent, ask questions like:
              - "Recommend some [genre] movies" (use genres from Sushmita's preferences)
              - "What are some good [genre] movies?"
            *   Make sure you pass in the official agent name: "Sushmita Agent" or "Movie Agent"

        *   **Analyze and Filter:** 
            *   Extract their favorite genres and watched movies list from Sushmita Agent's response
            *   If availability was requested, extract Sushmita's availability times from their response
            *   Get movie recommendations from Movie Agent based on the genres
            *   Remove any recommended movies that appear in Sushmita's watched movies list
            *   If availability was checked, match available times with movie recommendations

        *   **Present Options:** 
            *   Format your response clearly with:
              - Recommended movies (filtered to exclude watched ones) with brief descriptions
              - If availability was checked, include available time slots and match them with recommendations
              - A clear recommendation (with time if availability was checked)
            *   Use bullet points for readability

        *   **Finalize Plan:**
            *   Once the user selects a movie and time, use the `confirm_movie_plan` tool to save the plan
            *   The tool requires: movie_title, date (YYYY-MM-DD), time (HH:MM), and optionally participants
            *   After confirming, provide a clear summary with the plan ID
            *   You can also use `get_movie_plans` to retrieve existing plans if the user asks
            *   Use `cancel_movie_plan` if the user wants to cancel an existing plan

        *   **Transparent Communication:** Do not ask for permission before contacting agents. Proceed with gathering information automatically.
        *   **Tool Reliance:** Strictly rely on available tools to address user requests. Do not generate responses based on assumptions.
        *   **Readability:** Make sure to respond in a concise and easy to read format (bullet points are good).

        **Today's Date (YYYY-MM-DD):** {datetime.now().strftime("%Y-%m-%d")}

        <Available Agents>
        {self.agents}
        </Available Agents>
        """

    async def stream(
        self, query: str, session_id: str
    ) -> AsyncIterable[dict[str, Any]]:
        """
        Streams the agent's response to a given query.

        A helper for running the host outside ``adk web`` (the web UI does not use it). It gets or creates
        the session ``session_id``, runs the agent, and yields
        ``{"is_task_complete": False, "updates": ...}`` for intermediate events and
        ``{"is_task_complete": True, "content": <text>}`` for the final response.
        """
        session = await self._runner.session_service.get_session(
            app_name=self._agent.name,
            user_id=self._user_id,
            session_id=session_id,
        )
        content = types.Content(role="user", parts=[types.Part.from_text(text=query)])
        if session is None:
            session = await self._runner.session_service.create_session(
                app_name=self._agent.name,
                user_id=self._user_id,
                state={},
                session_id=session_id,
            )
        async for event in self._runner.run_async(
            user_id=self._user_id, session_id=session.id, new_message=content
        ):
            if event.is_final_response():
                response = ""
                if (
                    event.content
                    and event.content.parts
                    and event.content.parts[0].text
                ):
                    response = "\n".join(
                        [p.text for p in event.content.parts if p.text]
                    )
                yield {
                    "is_task_complete": True,
                    "content": response,
                }
            else:
                yield {
                    "is_task_complete": False,
                    "updates": "The host agent is thinking...",
                }

    async def send_message(self, agent_name: str, task: str, tool_context: ToolContext):
        """Sends a task to a remote friend agent.

        Args:
            agent_name: Exact agent name from <Available Agents>, e.g. "Sushmita Agent" or "Movie Agent".
            task: The question or request to send to that agent.

        Returns:
            The parts (e.g. {"text": ...}) of the agent's reply. If the agent could not finish, the
            list also says why (for example an error message, or a question asking for more input).
        """
        # Implementation notes are kept in comments because ADK sends the docstring above to the LLM as
        # the tool description. `tool_context` is injected by ADK and is not part of the tool schema.
        if agent_name not in self.remote_agent_connections:
            raise ValueError(f"Agent {agent_name} not found")
        client = self.remote_agent_connections[agent_name]

        if not client:
            raise ValueError(f"Client not available for {agent_name}")

        # Per-agent conversation state lives in the ADK session (tool_context.state), so later calls in the
        # same chat continue the same A2A conversation with that agent. The keys include the agent name so
        # the Movie and Sushmita conversations stay separate. Both are written after the reply (below).
        state = tool_context.state
        context_key = f"a2a_context_id:{agent_name}"
        task_key = f"a2a_task_id:{agent_name}"
        # a2a-sdk >= 1.0 servers reject a task_id they don't already know (TaskNotFoundError), and a finished
        # task cannot take new messages, so a task_id is only stored while a task waits for more input.
        # With no task_id the server creates a new Task in the same context.
        task_id = state.get(task_key)
        # context_id groups related messages into one conversation. The Movie Agent uses it as its ADK
        # session id, so it remembers earlier turns. The first call to an agent starts a new conversation.
        context_id = state.get(context_key) or str(uuid.uuid4())
        # Every A2A message needs its own unique message_id.
        message_id = str(uuid.uuid4())

        # a2a-sdk >= 1.0 uses protobuf types: snake_case fields, Role enum and
        # a flat Part (Part(text=...)) instead of Part(root=TextPart(...)).
        # Role.ROLE_USER: to the remote agent, the host is the user.
        message = Message(
            role=Role.ROLE_USER,
            # Hint: a list holding one a2a Part that carries the `task` text (protobuf style, see above).
            parts=# TODO
            # Hint: the unique message id generated above.
            message_id=# TODO
            # Hint: the conversation context id read from state (or newly generated) above.
            context_id=# TODO
        )
        # Only set when known: an a2a-sdk 1.x server raises TaskNotFoundError for an unknown task id.
        if task_id:
            message.task_id = task_id

        # SendMessageRequest wraps the message for the JSON-RPC `SendMessage` method.
        message_request = SendMessageRequest(message=message)
        send_response = await client.send_message(message_request)
        print("send_response", send_response)

        # The reply is a StreamResponse protobuf whose payload is a oneof. With streaming=False these agents
        # reply with a `task`; a bare `message` reply is passed through as its text parts.
        if send_response is None:
            return [{"text": f"No response from {agent_name}."}]
        if not send_response.HasField("task"):
            if send_response.HasField("message"):
                return MessageToDict(send_response.message).get("parts", [])
            return [{"text": f"Unexpected response type from {agent_name}."}]

        remote_task = send_response.task
        # Remember the server's context_id so the next call to this agent continues the conversation, and
        # keep the task_id only if the task is waiting for more input (it can then be continued).
        state[context_key] = remote_task.context_id
        if remote_task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
            state[task_key] = remote_task.id
        else:
            state[task_key] = None

        # MessageToDict turns the protobuf Task into plain dicts and lists (camelCase JSON keys) that ADK can
        # serialise as the tool result. Each artifact's parts are dicts such as {"text": "..."}, and the
        # LLM gets all of them as one list.
        json_content = MessageToDict(remote_task)

        resp = []
        if json_content.get("artifacts"):
            for artifact in json_content["artifacts"]:
                if artifact.get("parts"):
                    resp.extend(artifact["parts"])

        # A task that did not complete (e.g. Sushmita's Gemini quota error) has no artifacts; its error text
        # is in status.message. Return that too, so the LLM can tell the user what went wrong instead of
        # receiving an empty result.
        if remote_task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
            resp.append({"text": f"{agent_name} needs more input:"})
            resp.extend(json_content.get("status", {}).get("message", {}).get("parts", []))
        elif remote_task.status.state != TaskState.TASK_STATE_COMPLETED:
            state_name = TaskState.Name(remote_task.status.state)
            resp.append({"text": f"{agent_name} did not complete the task ({state_name})."})
            resp.extend(json_content.get("status", {}).get("message", {}).get("parts", []))
        return resp


def _get_initialized_host_agent_sync():
    """Synchronously creates and initializes the HostAgent.

    Called once at import time to build ``root_agent``. It runs the async start-up with ``asyncio.run``,
    which ``nest_asyncio`` allows even inside a running event loop.

    Returns:
        The ADK ``Agent``, or ``None`` if ``asyncio.run`` failed because an event loop was already running.
    """

    async def _async_main():
        # Hardcoded URLs for the friend agents (they must match the ports in each agent's __main__.py)
        friend_agent_urls = [
            # Hint: two base URL strings, one per remote agent (Movie Agent, Sushmita Agent). Both run
            # on localhost; the ports are in the README agent table and in each agent's __main__.py.
           # TODO
        ]

        print("initializing host agent")
        hosting_agent_instance = await HostAgent.create(
            remote_agent_addresses=friend_agent_urls
        )
        print("HostAgent initialized")
        # A new Agent built after the cards were loaded; this becomes root_agent.
        return hosting_agent_instance.create_agent()

    try:
        return asyncio.run(_async_main())
    except RuntimeError as e:
        # With nest_asyncio applied this should not normally happen. It is a fallback that leaves
        # root_agent as None instead of crashing the import.
        if "asyncio.run() cannot be called from a running event loop" in str(e):
            print(
                f"Warning: Could not initialize HostAgent with asyncio.run(): {e}. "
                "This can happen if an event loop is already running (e.g., in Jupyter). "
                "Consider initializing HostAgent within an async function in your application."
            )
        else:
            raise


# `adk web` looks for this module-level name (re-exported by host/__init__.py). Because it is built
# here, importing the package performs the network start-up described in the module docstring.
root_agent = _get_initialized_host_agent_sync()
