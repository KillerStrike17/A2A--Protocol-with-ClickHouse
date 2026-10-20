"""Sushmita Agent logic: a CrewAI crew that answers as Sushmita's personal assistant.

Role in the 3-agent system
    The Host Agent asks this agent (over A2A, port 10006) for Sushmita's availability, favourite movie
    genres and already-watched movies, and uses the answers to filter the Movie Agent's recommendations.
    This module holds only the CrewAI part: ``agent_executor.py`` adapts it to A2A and ``__main__.py``
    serves it (run ``python .`` from the ``sushmita_agent`` directory).

CrewAI pieces
    - Tools (``BaseTool`` subclasses): ``AvailabilityTool`` ("Calendar Availability Checker") reads the
      random calendar ``MY_CALENDAR``; ``MoviePreferencesTool`` ("Movie Preferences Manager") returns
      ``MOVIE_GENRE_PREFERENCES`` and ``WATCHED_MOVIES``. CrewAI gives the LLM each tool's ``name``,
      ``description`` and ``args_schema``.
    - ``Agent``: one assistant persona (role, goal, backstory) that can use both tools.
    - ``Task``: created for each question in ``SchedulingAgent.invoke``.
    - ``Crew``: runs that one task (``Process.sequential``). ``kickoff()`` is synchronous and blocks.

All data is in memory: the preferences are hard-coded and the calendar is regenerated at each start.

Environment variables
    ``GOOGLE_API_KEY`` (required): Gemini API key passed to the CrewAI ``LLM``.
    ``GEMINI_MODEL``: Gemini model name (default ``gemini-3.8-flash``); see ``CREWAI_GEMINI_MODEL``.
"""

import os
import random
from datetime import date, datetime, timedelta
from typing import Type

from crewai import LLM, Agent, Crew, Process, Task
from crewai.tools import BaseTool
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

# gemini-2.0-flash has been shut down; override with the GEMINI_MODEL env var.
# CrewAI routes Gemini models via the "gemini/" provider prefix.
# CrewAI's LLM picks the provider from that prefix (e.g. "gemini/gemini-3.8-flash"), so it is added
# unless GEMINI_MODEL already includes it. The Movie and Host agents use the bare name with ADK.
_GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
CREWAI_GEMINI_MODEL = (
    _GEMINI_MODEL if _GEMINI_MODEL.startswith("gemini/") else f"gemini/{_GEMINI_MODEL}"
)


def generate_calendar() -> dict[str, list[str]]:
    """Generates a random calendar for the next 7 days.

    Each day, from today to today + 6, gets 8 random free hourly slots between 08:00 and 20:00.

    Returns:
        A dict mapping "YYYY-MM-DD" to a sorted list of "HH:00" slots.
    """
    calendar = {}
    today = date.today()
    possible_times = [f"{h:02}:00" for h in range(8, 21)]  # 8 AM to 8 PM

    for i in range(7):
        current_date = today + timedelta(days=i)
        date_str = current_date.strftime("%Y-%m-%d")
        available_slots = sorted(random.sample(possible_times, 8))
        calendar[date_str] = available_slots
    print("---- Sushmita's Generated Calendar ----")
    print(calendar)
    print("---------------------------------")
    return calendar


# Generated once at import time, so Sushmita's availability changes every time the server restarts.
MY_CALENDAR = generate_calendar()

# Movie preferences and watched movies list
# Hard-coded demo data returned by MoviePreferencesTool. The Host Agent uses WATCHED_MOVIES to drop
# recommendations Sushmita has already seen.
MOVIE_GENRE_PREFERENCES = [
    "Action",
    "Comedy",
    "Drama",
    "Sci-Fi",
    "Thriller",
    "Romance",
]

WATCHED_MOVIES = [
    "The Matrix",
    "Inception",
    "Interstellar",
    "The Dark Knight",
    "Pulp Fiction",
    "Forrest Gump",
    "The Shawshank Redemption",
    "Titanic",
    "Avatar",
    "The Avengers",
]


class AvailabilityToolInput(BaseModel):
    """Input schema for AvailabilityTool."""

    date_range: str = Field(
        ...,
        description="The date or date range to check for availability, e.g., '2024-07-28' or '2024-07-28 to 2024-07-30'.",
    )


class AvailabilityTool(BaseTool):
    """CrewAI tool that reports Sushmita's free slots from ``MY_CALENDAR``.

    The ``name`` and ``description`` values below are what the LLM sees; ``args_schema`` defines and
    validates the tool's arguments.
    """

    name: str = "Calendar Availability Checker"
    description: str = (
        "Checks my availability for a given date or date range. "
        "Use this to find out when I am free."
    )
    args_schema: Type[BaseModel] = AvailabilityToolInput

    def _run(self, date_range: str) -> str:
        """Checks my availability for a given date range.

        Accepts "YYYY-MM-DD" or "YYYY-MM-DD to YYYY-MM-DD" (inclusive). Returns one line per day. Errors are
        returned as text, not raised, so the LLM can read them and retry.
        """
        # A single date gives a one-element list, so start and end are the same day.
        dates_to_check = [d.strip() for d in date_range.split("to")]
        start_date_str = dates_to_check[0]
        end_date_str = dates_to_check[-1]

        try:
            start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end = datetime.strptime(end_date_str, "%Y-%m-%d").date()

            if start > end:
                return (
                    "Invalid date range. The start date cannot be after the end date."
                )

            results = []
            delta = end - start
            for i in range(delta.days + 1):
                day = start + timedelta(days=i)
                date_str = day.strftime("%Y-%m-%d")
                # Days outside the generated 7-day window have no slots and are reported as unavailable.
                available_slots = MY_CALENDAR.get(date_str, [])
                if available_slots:
                    availability = f"On {date_str}, I am available at: {', '.join(available_slots)}."
                    results.append(availability)
                else:
                    results.append(f"I am not available on {date_str}.")

            return "\n".join(results)

        except ValueError:
            return (
                "I couldn't understand the date. "
                "Please ask to check availability for a date like 'YYYY-MM-DD'."
            )


class MoviePreferencesToolInput(BaseModel):
    """Input schema for MoviePreferencesTool."""

    query: str = Field(
        ...,
        description="The query about movie preferences - can ask about favorite genres or watched movies.",
    )


class MoviePreferencesTool(BaseTool):
    """CrewAI tool that returns Sushmita's favourite genres and/or her watched-movies list.

    The ``name`` and ``description`` values below are what the LLM sees.
    """

    name: str = "Movie Preferences Manager"
    description: str = (
        "Gets my movie genre preferences and list of movies I've already watched. "
        "Use this to find out what genres of movies I like and which movies I've seen."
    )
    args_schema: Type[BaseModel] = MoviePreferencesToolInput

    def _run(self, query: str) -> str:
        """Returns movie genre preferences and watched movies list."""
        # Simple keyword routing: genre questions, watched-list questions, otherwise both.
        query_lower = query.lower()
        
        if "genre" in query_lower or "preference" in query_lower or "like" in query_lower:
            genres_str = ", ".join(MOVIE_GENRE_PREFERENCES)
            return (
                f"My favorite movie genres are: {genres_str}. "
                f"I enjoy watching movies in these genres."
            )
        elif "watched" in query_lower or "seen" in query_lower or "already" in query_lower:
            movies_str = ", ".join(WATCHED_MOVIES)
            return (
                f"Here are the movies I've already watched: {movies_str}. "
                f"Please don't recommend these movies as I've already seen them."
            )
        else:
            # Return both if query is general
            genres_str = ", ".join(MOVIE_GENRE_PREFERENCES)
            movies_str = ", ".join(WATCHED_MOVIES)
            return (
                f"My favorite movie genres are: {genres_str}. "
                f"Movies I've already watched: {movies_str}."
            )


class SchedulingAgent:
    """Agent that handles scheduling tasks.

    Wraps a CrewAI ``Agent`` (Sushmita's personal assistant, with both tools). ``invoke`` builds a new
    ``Task`` and ``Crew`` for each question and runs them synchronously.

    Attributes:
        SUPPORTED_CONTENT_TYPES: MIME types advertised as input/output modes in the agent card.
        llm: The CrewAI ``LLM`` for Gemini.
        scheduling_assistant: The CrewAI ``Agent``.
    """

    SUPPORTED_CONTENT_TYPES = ["text/plain"]

    def __init__(self):
        """Initializes the SchedulingAgent.

        Raises:
            ValueError: If ``GOOGLE_API_KEY`` is not set.
        """
        if os.getenv("GOOGLE_API_KEY"):
            # CrewAI's LLM wrapper; the model string needs the "gemini/" prefix (see CREWAI_GEMINI_MODEL).
            self.llm = LLM(
                model=CREWAI_GEMINI_MODEL,
                api_key=os.getenv("GOOGLE_API_KEY"),
            )
        else:
            raise ValueError("GOOGLE_API_KEY environment variable not set.")

        # role, goal and backstory form the agent's persona prompt. verbose=True prints its reasoning to
        # the console. allow_delegation=False: there is no other agent in this crew to delegate to.
        self.scheduling_assistant = Agent(
            role="Personal Assistant for Scheduling and Movie Preferences",
            goal=(
                "Check my calendar for availability and provide information about "
                "my movie genre preferences and watched movies list."
            ),
            backstory=(
                "You are a highly efficient and polite assistant. You have two main responsibilities: "
                "1) Manage my calendar and answer questions about my availability using the "
                "Calendar Availability Checker tool. "
                "2) Provide information about my movie preferences - specifically my favorite genres "
                "and the list of movies I've already watched using the Movie Preferences Manager tool. "
                "When asked about movie watching availability, check my calendar first, then provide "
                "my movie genre preferences. Always be helpful and concise."
            ),
            verbose=True,
            allow_delegation=False,
            tools=[AvailabilityTool(), MoviePreferencesTool()],
            llm=self.llm,
        )

    def invoke(self, question: str) -> str:
        """Kicks off the crew to answer questions about availability or movie preferences.

        Blocking: ``crew.kickoff()`` runs the LLM and tool loop synchronously, so the A2A executor calls
        this through ``asyncio.to_thread``.

        Args:
            question: The user's text from the A2A message.

        Returns:
            The crew's final answer as a string.
        """
        # The question and today's date go into the prompt so the agent can resolve "tomorrow" or
        # "this week" into concrete dates for the availability tool.
        task_description = (
            f"Answer the user's question. The user asked: '{question}'. "
            f"Today's date is {date.today().strftime('%Y-%m-%d')}. "
            f"If the question is about availability, use the Calendar Availability Checker tool. "
            f"If the question is about movie preferences, genres, or watched movies, use the Movie Preferences Manager tool. "
            f"If the question is about watching movies, check both availability and movie preferences."
        )

        check_availability_task = Task(
            description=task_description,
            expected_output=(
                "A polite and concise answer to the user's question. "
                "If about availability, provide calendar information. "
                "If about movies, provide genre preferences and/or watched movies list. "
                "If about watching movies together, provide both availability and movie preferences."
            ),
            agent=self.scheduling_assistant,
        )

        # A crew with one agent and one task; Process.sequential runs tasks in list order.
        crew = Crew(
            agents=[self.scheduling_assistant],
            tasks=[check_availability_task],
            process=Process.sequential,
            verbose=True,
        )
        # kickoff() returns a CrewOutput; str() gives the final answer text.
        result = crew.kickoff()
        return str(result)
