"""This file serves as the main entry point for the application.

It initializes the A2A server, defines the agent's capabilities,
and starts the server to handle incoming requests.

Sushmita Agent server: the CrewAI agent from ``agent.py``, exposed over A2A. It is one of the two remote
agents the Host Agent talks to.

How it is run
    ``cd Solution/sushmita_agent`` then ``python .``. Python runs this ``__main__.py`` with the directory
    on ``sys.path``, so ``agent`` and ``agent_executor`` are imported as top-level modules. The server
    listens on ``http://localhost:10006`` and serves:

    - the agent card at the well-known agent-card URL (``create_agent_card_routes``). The Host Agent's
      ``A2ACardResolver`` downloads it at start-up.
    - JSON-RPC (``SendMessage`` and the other A2A methods) at ``/`` (``create_jsonrpc_routes``), handled by
      ``DefaultRequestHandler`` and ``SchedulingAgentExecutor``. Tasks are kept in an ``InMemoryTaskStore``.

Environment variables
    ``GOOGLE_API_KEY`` (required): checked here before start-up and used by the CrewAI LLM.
    ``GEMINI_MODEL``: Gemini model name, read in ``agent.py``.
    ``HOST_OVERRIDE``: a public URL to advertise in the agent card instead of ``http://localhost:10006/``,
        e.g. when the server sits behind a tunnel or reverse proxy. Clients send requests to the card's URL.
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
from agent import SchedulingAgent
from agent_executor import SchedulingAgentExecutor
from dotenv import load_dotenv
from starlette.applications import Starlette

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MissingAPIKeyError(Exception):
    """Raised at start-up when GOOGLE_API_KEY is not set."""


def main():
    """Entry point for Sushmita's Scheduling Agent.

    Builds the agent card, request handler and Starlette routes, then runs uvicorn until stopped.
    Any start-up error is logged and the process exits with status 1.
    """
    # Bind address and port. The Host Agent's hardcoded URL list expects http://localhost:10006.
    # Hint: the hostname to bind to (a string); everything in this demo runs on the local machine.
    host = # TODO 
    # Hint: the integer port the Host Agent expects for Sushmita (see the README agent table).
    port = # TODO
    try:
        if not os.getenv("GOOGLE_API_KEY"):
            raise MissingAPIKeyError("GOOGLE_API_KEY environment variable not set.")

        # streaming=False: only non-streaming SendMessage is supported; each reply is one finished Task.
        capabilities = AgentCapabilities(streaming=False)
        # Skills are descriptive metadata in the agent card that clients can show to a user or an LLM.
        # They do not route requests: the CrewAI agent decides which tool to use.
        skill1 = AgentSkill(
            id="availability_checker",
            name="Availability Checker",
            description="Check my calendar to see when I'm available.",
            tags=["schedule", "availability", "calendar"],
            examples=[
                "Are you free tomorrow?",
                "When are you available this week?",
            ],
        )
        skill2 = AgentSkill(
            id="movie_preferences",
            name="Movie Preferences",
            description="Get my favorite movie genres and list of movies I've already watched.",
            tags=["movies", "preferences", "genres"],
            examples=[
                "What genres of movies do you like?",
                "What movies have you already watched?",
                "Are you available to watch a movie and what genres do you prefer?",
            ],
        )

        # HOST_OVERRIDE advertises a public URL (tunnel or proxy) instead of the local one. a2a-sdk
        # clients send requests to the URL in the card, so it must be reachable by callers.
        agent_host_url = os.getenv("HOST_OVERRIDE") or f"http://{host}:{port}/"
        # The agent card describes this agent to clients. The Host Agent keys its connections by `name`
        # (the LLM passes "Sushmita Agent" to send_message) and shows `description` to its LLM.
        agent_card = AgentCard(
            # Hint: the agent's display name; the Host Agent's instruction refers to it by this exact name.
            name=# TODO,
            description=(
               # Hint: one or two sentences on what this agent offers (availability and movie preferences).
               # TODO
            ),
            # a2a-sdk >= 1.0: `url` moved into `supported_interfaces`.
            supported_interfaces=[
                AgentInterface(
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                    # Hint: the URL computed above (it honours HOST_OVERRIDE).
                    url=# TODO
                )
            ],
            version="1.0.0",
            default_input_modes=SchedulingAgent.SUPPORTED_CONTENT_TYPES,
            default_output_modes=SchedulingAgent.SUPPORTED_CONTENT_TYPES,
            # Hint: the capabilities object created above.
            capabilities=# TODO,
            # Hint: a list of both skills defined above.
            skills=# TODO,
            # skills=[skill1],
        )

        # a2a-sdk >= 1.0: the request handler takes the agent card, and
        # A2AStarletteApplication was replaced by route factory functions.
        # The handler runs the executor for each request; tasks live in memory and are lost on restart.
        request_handler = DefaultRequestHandler(
            # Hint: an instance of the executor class imported from agent_executor.py.
            agent_executor=# TODO
            task_store=InMemoryTaskStore(),
            # Hint: the agent card built above.
            agent_card=# TODO
        )
        # Agent-card route plus the JSON-RPC endpoint at "/", served by uvicorn as a plain Starlette app.
        routes = []
        routes.extend(create_agent_card_routes(agent_card))
        routes.extend(create_jsonrpc_routes(request_handler, rpc_url="/"))
        server = Starlette(routes=routes)

        uvicorn.run(server, host=host, port=port)

    except MissingAPIKeyError as e:
        logger.error(f"Error: {e}")
        exit(1)
    except Exception as e:
        logger.error(f"An error occurred during server startup: {e}")
        exit(1)


if __name__ == "__main__":
    main()
