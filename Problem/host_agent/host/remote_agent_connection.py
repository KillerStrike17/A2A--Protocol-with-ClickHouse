"""A2A client wrapper the Host Agent uses to talk to one remote agent.

``host/agent.py`` downloads each remote agent's card with ``A2ACardResolver`` and wraps it in a
``RemoteAgentConnections`` (one for the Movie Agent on :10005, one for the Sushmita Agent on :10006).
Each instance owns an ``httpx.AsyncClient`` and an a2a-sdk 1.x client created by ``ClientFactory`` from
the card. ``send_message`` sends a JSON-RPC ``SendMessage`` request and returns the reply, which is
normally the finished ``Task``.

This module is not run directly; ``host/agent.py`` imports it. ``load_dotenv()`` is called to match the
other modules, but this module does not read any environment variables itself.
"""

from typing import Callable

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)
from dotenv import load_dotenv

load_dotenv()

# Type aliases kept from the original A2A samples for task-update callbacks. This demo does not use
# them; they list the event types a streaming client can receive (Task, status and artifact updates).
TaskCallbackArg = Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent
TaskUpdateCallback = Callable[[TaskCallbackArg, AgentCard], Task]


class RemoteAgentConnections:
    """A class to hold the connections to the remote agents.

    One instance per remote agent.

    Attributes:
        agent_client: The a2a-sdk ``Client`` that ``ClientFactory`` built for ``card``.
        agent_url: The base URL the card was fetched from (e.g. ``http://localhost:10005``).
        card: The remote agent's ``AgentCard`` (name, description, skills, endpoint URL).
        conversation_name, conversation, pending_tasks: Placeholders kept from the original samples;
            unused in this demo.
    """

    def __init__(self, agent_card: AgentCard, agent_url: str):
        """Builds the A2A client for ``agent_card``.

        Args:
            agent_card: The card downloaded from the remote agent.
            agent_url: The base URL the card was downloaded from. It is stored for reference only; requests
                go to the endpoint listed in the card.
        """
        print(f"agent_card: {agent_card}")
        print(f"agent_url: {agent_url}")
        # A 30 s timeout leaves time for the remote agent's Gemini (and ClickHouse) calls before it replies.
        self._httpx_client = httpx.AsyncClient(timeout=30)
        # a2a-sdk >= 1.0: the legacy `A2AClient` was removed. Clients are now
        # built by `ClientFactory` from the agent card (the endpoint URL comes
        # from `agent_card.supported_interfaces`). Streaming is disabled so a
        # single `message/send` call returns the final Task, as before.
        # ClientConfig(streaming=False) makes the client use the non-streaming JSON-RPC `SendMessage`
        # method (the remote agents advertise `streaming=False` in their cards) and reuse our httpx client.
        self.agent_client = ClientFactory(
            ClientConfig(streaming=False, httpx_client=self._httpx_client)
        ).create(agent_card)
        self.agent_url = agent_url
        self.card = agent_card
        self.conversation_name = None
        self.conversation = None
        self.pending_tasks = set()

    def get_agent(self) -> AgentCard:
        """Returns the remote agent's card."""
        return self.card

    async def send_message(
        self, message_request: SendMessageRequest
    ) -> StreamResponse | None:
        """Sends a message and returns the last response (normally the final Task).

        In a2a-sdk 1.x, ``Client.send_message`` is an async iterator for streaming and non-streaming
        clients alike. With ``streaming=False`` it yields a single ``StreamResponse`` that holds a ``task``
        (or a ``message``). A streaming client would yield status and artifact updates before the final
        state. Keeping only the last item works in both cases, so the caller always gets the final result.

        Args:
            message_request: The ``SendMessageRequest`` wrapping the a2a ``Message`` to send.

        Returns:
            The last ``StreamResponse`` received, or ``None`` if the server sent nothing.
        """
        last_response: StreamResponse | None = None
        async for response in self.agent_client.send_message(message_request):
            last_response = response
        return last_response
