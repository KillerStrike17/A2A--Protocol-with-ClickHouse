"""Host Agent app directory for ``adk web``.

Run ``adk web --port 8000`` from this directory (``Solution/host_agent``) after the Movie Agent
(port 10005) and the Sushmita Agent (port 10006) are running. ``adk web`` treats each sub-package that
exposes a ``root_agent`` as an app; here that is the ``host`` package, listed as "host" in the web UI's
app dropdown. This file only marks the directory as a Python package and contains no code.
"""
