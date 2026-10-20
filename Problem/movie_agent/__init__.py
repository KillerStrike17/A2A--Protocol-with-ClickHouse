"""Movie Agent package: an A2A server wrapping a Google ADK agent backed by ClickHouse.

The Movie Agent recommends movies from the ClickHouse IMDB example dataset. The Host Agent calls it
over the A2A protocol (JSON-RPC ``SendMessage``) on http://localhost:10005.

Modules:
    agent: the ADK ``LlmAgent`` (Gemini) and its ClickHouse query tools.
    agent_executor: ``MovieAgentExecutor``, which bridges A2A requests to the ADK ``Runner``.
    __main__: builds the Agent Card and starts the Starlette/uvicorn server (``python .``).

The modules import each other with plain ``from agent import ...`` statements because the server
is started from inside this directory, so this file is intentionally empty apart from this
docstring.
"""
