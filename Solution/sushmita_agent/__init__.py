"""Sushmita Agent: a CrewAI agent served over the A2A protocol on port 10006.

Start it with ``python .`` from this directory; that runs ``__main__.py``. The modules import each
other as top-level modules (``from agent import ...``), so the package is not imported by name.

- ``agent.py``: the CrewAI crew (tools, agent, task) that answers questions.
- ``agent_executor.py``: adapts the crew to the a2a-sdk ``AgentExecutor`` interface.
- ``__main__.py``: builds the agent card and starts the A2A HTTP server.
"""
