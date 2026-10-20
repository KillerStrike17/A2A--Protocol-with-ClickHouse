"""The Host agent package (the "host" app that ``adk web`` loads).

``adk web`` imports this package and looks for a module-level ``root_agent``; re-exporting it from
``.agent`` is what makes the Host Agent discoverable. Importing ``.agent`` runs its start-up code: it
downloads the Movie Agent (http://localhost:10005) and Sushmita Agent (http://localhost:10006) agent
cards, so start those two servers before ``adk web``.
"""

# ADK discovers the agent through this exact name: `root_agent`.
from .agent import root_agent
