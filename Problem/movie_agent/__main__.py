"""Entry point: serves the Movie Agent as an A2A server on http://localhost:10005.

Role in the 3-agent system
--------------------------
The Movie Agent is one of the two "remote" agents the Host Agent talks to over the A2A protocol
(the other is the CrewAI Sushmita Agent on port 10006). This module publishes what the agent can
do (its *Agent Card*) and exposes the JSON-RPC endpoint that receives ``SendMessage`` requests.

How it is run
-------------
From the repo root, with the virtualenv active::

    cd Problem/movie_agent
    python .

``python .`` runs this ``__main__.py``; the directory is on ``sys.path``, which is why the plain
``from agent import ...`` / ``from agent_executor import ...`` imports work. Wait for
"Uvicorn running on http://localhost:10005", then start the Host Agent (it fetches this card at
startup). Check it with http://localhost:10005/.well-known/agent-card.json.

Request flow
------------
Host Agent --JSON-RPC POST /--> ``DefaultRequestHandler`` --> ``MovieAgentExecutor.execute()``
--> ADK ``Runner`` --> Gemini + ClickHouse tools (``agent.py``) --> task events --> response.

Exercise
--------
This is the learner version: the ``AgentSkill`` fields, the ``AgentCard`` fields and the
interface URL contain ``# TODO`` gaps (marked with ``HINT`` comments), so the file does not compile
until they are filled in. ``Solution/movie_agent/__main__.py`` is the reference implementation.

Environment variables (read from ``.env``)
------------------------------------------
* ``GOOGLE_API_KEY``: Gemini API key, required unless ``GOOGLE_GENAI_USE_VERTEXAI=TRUE``.
* ``GOOGLE_GENAI_USE_VERTEXAI``: ``TRUE`` to authenticate through Vertex AI instead.
* The ``CLICKHOUSE_*``, ``MOVIE_AGENT_MODEL`` / ``GEMINI_MODEL`` and ``MOVIE_MIN_CAST_SIZE``
  variables are read by ``agent.py``.
"""
import logging
import os

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)
from agent import create_agent
from agent_executor import MovieAgentExecutor
from dotenv import load_dotenv
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from starlette.applications import Starlette

# Load .env so GOOGLE_API_KEY etc. are visible to os.getenv() and to ADK / google-genai.
# (agent.py also calls load_dotenv() when it is imported above; calling it twice is harmless.)
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MissingAPIKeyError(Exception):
    """Exception for missing API key."""

    pass


def main():
    """Starts the agent server.

    Builds the Agent Card, the ADK runner and the A2A request handler, mounts the A2A routes on a
    Starlette app and serves it with uvicorn until Ctrl+C. Exits with status 1 if the Gemini
    credentials are missing or startup fails.
    """
    # The address advertised in the Agent Card must match where uvicorn listens.
    host = "localhost"
    port = 10005
    try:
        # Check for API key only if Vertex AI is not configured
        # (fail fast at startup rather than on the first request, when Gemini would reject it).
        if not os.getenv("GOOGLE_GENAI_USE_VERTEXAI") == "TRUE":
            if not os.getenv("GOOGLE_API_KEY"):
                raise MissingAPIKeyError(
                    "GOOGLE_API_KEY environment variable not set and GOOGLE_GENAI_USE_VERTEXAI is not TRUE."
                )

        # Optional protocol features this server supports; streaming=True means clients may use
        # SendStreamingMessage to receive the WORKING updates as they happen.
        capabilities = AgentCapabilities(streaming=True)
        # An AgentSkill describes one thing the agent can do. The Host Agent's LLM reads the
        # skills (description, tags, example prompts) to decide when to delegate to this agent.
        skill = AgentSkill(
            # HINT: a short machine-readable id for the skill, e.g. "something_recommendations".
            id=# TODO,
            # HINT: a human-readable skill name.
            name=# TODO,
            # HINT: one sentence on what the skill does (search criteria, data source); the Host
            # Agent's LLM reads it to decide when to call this agent.
            description=# TODO,
            # HINT: a list of short keyword strings.
            tags=# TODO,
            examples=[
                # HINT: a few example user prompts as strings (genre, actor, year, keyword...).
                # TODO
            ],
        )
        # The AgentCard is the agent's public "business card", served as JSON at
        # /.well-known/agent-card.json. Clients fetch it to discover the agent's name, purpose,
        # skills, supported input/output media types and where/how to send messages.
        agent_card = AgentCard(
            # HINT: the display name clients will see (it is also used as the ADK app_name below).
            name=# TODO,
            # HINT: one or two sentences describing the agent and the data it uses.
            description=# TODO,
            # a2a-sdk >= 1.0: `url` moved into `supported_interfaces`.
            # Each AgentInterface is one endpoint + transport; here a single JSON-RPC endpoint
            # at the server root speaking A2A protocol version 1.0.
            supported_interfaces=[
                AgentInterface(
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                    # HINT: an f-string building the http URL of this server's root from host and port.
                    url=# TODO,
                )
            ],
            version="1.0.0",
            # HINT: lists of MIME types; this agent consumes and produces plain text only.
            default_input_modes=# TODO,
            default_output_modes=# TODO,
            # HINT: the AgentCapabilities object and a list with the AgentSkill built above.
            capabilities=# TODO,
            skills=# TODO,
        )

        # The ADK LlmAgent (Gemini + ClickHouse tools) from agent.py.
        adk_agent = create_agent()
        # The Runner drives the agent: it loads the session, calls the model, executes tools and
        # yields events. All three services are in-memory, so sessions (conversation history),
        # artifacts and memory are lost when the server restarts. app_name scopes the sessions.
        runner = Runner(
            app_name=agent_card.name,
            agent=adk_agent,
            artifact_service=InMemoryArtifactService(),
            session_service=InMemorySessionService(),
            memory_service=InMemoryMemoryService(),
        )
        # Adapter that turns A2A requests into runner calls (see agent_executor.py).
        agent_executor = MovieAgentExecutor(runner)

        # a2a-sdk >= 1.0: the request handler takes the agent card, and
        # A2AStarletteApplication was replaced by route factory functions.
        # DefaultRequestHandler implements the A2A methods (SendMessage, SendStreamingMessage,
        # GetTask, CancelTask, ...): it creates the RequestContext / EventQueue, calls the
        # executor and stores task state. InMemoryTaskStore keeps tasks in a dict, so GetTask
        # only knows tasks created since the last restart.
        request_handler = DefaultRequestHandler(
            agent_executor=agent_executor,
            task_store=InMemoryTaskStore(),
            agent_card=agent_card,
        )
        # Two groups of Starlette routes:
        # * GET /.well-known/agent-card.json -> the agent card (A2A discovery)
        # * POST / -> the JSON-RPC 2.0 endpoint (e.g. {"method": "SendMessage", ...})
        routes = []
        routes.extend(create_agent_card_routes(agent_card))
        routes.extend(create_jsonrpc_routes(request_handler, rpc_url="/"))
        server = Starlette(routes=routes)

        # Blocks until the process is stopped (Ctrl+C).
        uvicorn.run(server, host=host, port=port)
    except MissingAPIKeyError as e:
        logger.error(f"Error: {e}")
        exit(1)
    except Exception as e:
        logger.error(f"An error occurred during server startup: {e}")
        exit(1)


if __name__ == "__main__":
    main()
