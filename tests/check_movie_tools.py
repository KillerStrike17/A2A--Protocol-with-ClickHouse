"""
Smoke test for the Movie Agent's ClickHouse tools (no Gemini calls, no API credits needed).

Runs every tool in Solution/movie_agent/agent.py against the ClickHouse server
configured in .env and checks that each one returns results.

Usage (from the repo root, with the virtualenv active):
    python tests/check_movie_tools.py

Expected output ends with "6/6 checks passed" (exit code 0). Any failure
exits with code 1. Each tool opens its own ClickHouse connection, so expect
roughly a couple of seconds per check against the public SQL playground.

This test only exercises the plain Python tool functions; it does not start
the A2A server or call the LLM. Use tests/send_a2a_message.py for that.
"""
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Repo root (this file is <root>/tests/check_movie_tools.py).
ROOT = Path(__file__).resolve().parents[1]
# Load the repo-root .env explicitly, whatever the current directory is.
load_dotenv(ROOT / ".env")
# Make Solution/movie_agent importable so "import agent" picks up its agent.py.
sys.path.insert(0, str(ROOT / "Solution" / "movie_agent"))

import agent  # noqa: E402

# (tool, argument, text the result must contain)
# The expected strings are well-known titles/names in the IMDB dataset. The
# "The Matrix" and "The Godfather" cases also check the "Matrix, The" title
# conversion in both directions.
CHECKS = [
    (agent.recommend_movies_by_genre, "Sci-Fi", "Star Wars"),
    (agent.recommend_movies_by_actor, "Tom Hanks", "Forrest Gump"),
    (agent.recommend_movies_by_director, "Steven Spielberg", "Schindler's List"),
    (agent.recommend_movies_by_year, 1999, "Fight Club"),
    (agent.search_movies_by_keyword, "matrix", "The Matrix"),
    (agent.get_movie_details, "The Godfather", "Francis Ford Coppola"),
]


def main() -> int:
    """Run every check, print each result with timing, and return the exit code."""
    failures = 0
    for tool, arg, expected in CHECKS:
        started = time.perf_counter()
        # get_movie_details has no limit parameter; the list tools are asked for 5 rows.
        result = tool(arg) if tool is agent.get_movie_details else tool(arg, limit=5)
        elapsed = time.perf_counter() - started
        # Tools never raise: query errors come back as strings starting with "Error".
        ok = expected in result and not result.startswith("Error")
        failures += not ok
        print(f"\n[{'PASS' if ok else 'FAIL'}] {tool.__name__}({arg!r}) in {elapsed:.1f}s")
        print(result)
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
