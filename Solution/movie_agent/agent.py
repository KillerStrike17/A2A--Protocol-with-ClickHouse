"""Movie Agent: the Google ADK agent and its ClickHouse-backed tools.

Role in the 3-agent system
--------------------------
This module defines the "brain" of the Movie Agent, one of the three agents in the demo:

* **Movie Agent** (this package, Google ADK, port 10005) recommends movies from the IMDB example
  dataset stored in ClickHouse.
* **Sushmita Agent** (CrewAI, port 10006) knows Sushmita's availability and taste.
* **Host Agent** (Google ADK, ``adk web`` on port 8000) talks to the user and calls the other two
  agents over the A2A protocol.

``create_agent()`` builds an ADK ``LlmAgent`` (a Gemini model plus six plain-Python tools). The
tools run parameterised SQL against ClickHouse through ``clickhouse-connect`` and return short,
human-readable text that Gemini turns into an answer. This module knows nothing about A2A: the
server in ``__main__.py`` wraps the agent in an ADK ``Runner`` and ``agent_executor.py`` bridges
that runner to the A2A protocol.

How it is run
-------------
Not run directly. It is imported by ``__main__.py`` (``cd Solution/movie_agent && python .``) and
by ``tests/check_movie_tools.py``, which calls every tool against ClickHouse without Gemini.

Environment variables (read from ``.env`` via python-dotenv)
------------------------------------------------------------
* ``CLICKHOUSE_HOST`` / ``CLICKHOUSE_PORT`` / ``CLICKHOUSE_USERNAME`` / ``CLICKHOUSE_PASSWORD`` /
  ``CLICKHOUSE_DATABASE`` / ``CLICKHOUSE_SECURE``: where the IMDB tables live (defaults: a local,
  unsecured server on port 8123 with the ``imdb`` database).
* ``MOVIE_AGENT_MODEL`` (falls back to ``GEMINI_MODEL``): the Gemini model the agent uses.
* ``MOVIE_MIN_CAST_SIZE`` (default 30): popularity threshold for genre/year recommendations.
* ``GOOGLE_API_KEY`` / ``GOOGLE_GENAI_USE_VERTEXAI`` are read by ADK itself, not by this module.

IMDB dataset quirks handled here
--------------------------------
The ClickHouse IMDB example dataset (tables ``movies``, ``genres``, ``roles``, ``actors``,
``directors``, ``movie_directors``; movies from 1888-2008) has a few oddities:

* **Titles with a leading article are stored inverted**: "The Matrix" is ``"Matrix, The"``.
  ``to_display_title`` / ``to_imdb_title`` convert between the two forms.
* **Namesakes are disambiguated with a Roman-numeral suffix** in ``first_name``, e.g.
  ``"Chris (I)"``. ``PERSON_NAME`` strips that suffix so "Chris Evans" matches.
* **``rank`` is the IMDb rating (0-10) and 0 means "unrated"**, so recommendation queries filter
  ``rank > 0`` and outputs show "N/A" for a falsy rating.
* **There are no vote counts**, so the number of credited actors (rows in ``roles``) is used as a
  popularity proxy: big productions have big casts. See ``MIN_CAST_SIZE`` and
  ``FEATURE_CAST_SIZE``.
* **Many titles are shorts, documentaries or adult films** with very high ratings from very few
  voters; general recommendations exclude the ``Short``, ``Documentary`` and ``Adult`` genres.

Query design notes
------------------
* **Server-side parameter binding.** Every user-supplied value is passed as a ClickHouse query
  parameter written ``{name:Type}`` in the SQL (e.g. ``{genre:String}``, ``{limit:UInt32}``) and
  supplied via ``client.query(..., parameters={...})``. ClickHouse substitutes and type-checks the
  value itself, so text coming from the LLM can never be interpreted as SQL (no SQL injection).
  In f-strings the braces are doubled (``{{actor_name:String}}``) so Python leaves them alone and
  only ``{PERSON_NAME}`` is interpolated.
* **``IN`` subqueries instead of joins for filtering.** ClickHouse evaluates ``x IN (SELECT ...)``
  by building a hash set from the subquery first, then filtering the outer table with it. Using
  ``IN`` for "movies of this genre/actor/director" filters rows *before* any join touches the
  large tables (``roles`` has ~3.4M rows), instead of joining everything and filtering afterwards.
* **One client per tool call.** Each tool opens a client, runs its queries, and closes it in a
  ``finally`` block. Tools are invoked rarely (a few per conversation) and possibly concurrently
  by the ADK runner, so a short-lived client avoids sharing one HTTP session across async tasks,
  avoids stale/timed-out connections in a long-running server, and never leaks connections.
"""
import os
from typing import Optional
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
import clickhouse_connect
# Load .env into os.environ before any os.getenv() call below (no-op if the file is absent).
load_dotenv()
# ClickHouse connection parameters from environment variables
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "localhost")
# Accepts "1", "true" or "yes" (any case) to enable TLS; ClickHouse Cloud and the playground need it.
CLICKHOUSE_SECURE = os.getenv("CLICKHOUSE_SECURE", "false").strip().lower() in ("1", "true", "yes")
# 8123 = HTTP (local), 8443 = HTTPS (ClickHouse Cloud)
# `or` (not a getenv default) so that an empty CLICKHOUSE_PORT= line in .env also falls back.
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT") or (8443 if CLICKHOUSE_SECURE else 8123))
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USERNAME", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "imdb")
# Gemini model used by the agent (override with MOVIE_AGENT_MODEL, falls back to GEMINI_MODEL)
MOVIE_AGENT_MODEL = os.getenv("MOVIE_AGENT_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
# The IMDB dataset has no vote counts, so cast size is used as a popularity proxy:
# genre/year recommendations only include movies with at least this many credited actors.
# 30 is high enough to drop obscure titles rated 9.8 by a handful of voters, yet low enough to keep
# classics with modest casts.
MIN_CAST_SIZE = int(os.getenv("MOVIE_MIN_CAST_SIZE", "30"))
# Actor/director filmographies list feature films (at least this many credited actors) before shorts.
# This is a sort key, not a filter: a person's shorts and TV work still appear after their features.
FEATURE_CAST_SIZE = 10
# global client
# (Intentionally none: each tool creates and closes its own client; see the module docstring.)

# Logged once at import time so it is obvious which ClickHouse server the agent talks to.
print("ClickHouse host:", f"{CLICKHOUSE_HOST}:{CLICKHOUSE_PORT} (secure={CLICKHOUSE_SECURE})")

# IMDB titles move a leading article to the end: "The Matrix" is stored as "Matrix, The".
ARTICLES = ("The", "A", "An")

# Display name for actors/directors; IMDB disambiguates namesakes as "Chris (I)".
# This is a SQL expression (spliced into queries with an f-string, never user input):
# splitByString(' (', first_name)[1] keeps the part before " (" (ClickHouse arrays are 1-based),
# so first_name "Chris (I)" + last_name "Evans" becomes "Chris Evans".
PERSON_NAME = "concat(splitByString(' (', first_name)[1], ' ', last_name)"


def to_display_title(name: str) -> str:
    """Converts an IMDB title like "Matrix, The" into "The Matrix".

    Titles without a trailing ", The" / ", A" / ", An" are returned unchanged.
    """
    for article in ARTICLES:
        suffix = f", {article}"
        if name.endswith(suffix):
            return f"{article} {name[:-len(suffix)]}"
    return name


def to_imdb_title(title: str) -> str:
    """Converts a title like "The Matrix" into IMDB's "Matrix, The" form.

    The article match is case-insensitive ("the matrix" -> "matrix, The"); the SQL lowercases both
    sides anyway. Titles without a leading article are returned unchanged.
    """
    for article in ARTICLES:
        prefix = f"{article} "
        if title.lower().startswith(prefix.lower()):
            return f"{title[len(prefix):]}, {article}"
    return title


def get_clickhouse_client():
    """Get a ClickHouse client instance.

    Opens a new HTTP(S) client from the CLICKHOUSE_* settings and pings the server so connection
    problems surface immediately with a clear message. The caller owns the client and must
    ``close()`` it (every tool does so in a ``finally`` block).

    Raises:
        ConnectionError: if the server does not answer the ping.
        Exception: any error raised by clickhouse-connect while connecting (bad host, auth, TLS).
    """
    # clickhouse-connect talks to ClickHouse over its HTTP interface (8123 / 8443 with secure=True).
    client = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DATABASE,
        secure=CLICKHOUSE_SECURE,
    )
    try:
        print("Checking connectivity")
        # ping() returns False instead of raising when the server is unreachable.
        if not client.ping():
            raise ConnectionError(f"Unable to ping ClickHouse at {CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}")
        print("ClickHouse connection successful")
    except Exception as e:
        print(f"ClickHouse connection error: {e}")
        # Release the client's HTTP connection pool before propagating the error.
        client.close()
        raise
    return client


# ---------------------------------------------------------------------------------------------
# Agent tools
#
# ADK turns each function below into a Gemini function declaration: the function name, the type
# hints and the docstring (summary + Args) become the tool schema the model sees. Docstrings are
# therefore written for the LLM and kept short; implementation notes live in comments instead.
#
# Every tool returns a plain string, never raises: errors are returned as "Error ..." text so the
# model can tell the user what went wrong instead of the whole agent run failing.
# ---------------------------------------------------------------------------------------------


def recommend_movies_by_genre(genre: str, limit: int = 10) -> str:
    """
    Recommends highly rated, well-known movies in a specific genre.

    Shorts, documentaries and adult titles are excluded unless that is the requested genre.

    Args:
        genre: The genre to search for (e.g., "Action", "Comedy", "Drama", "Sci-Fi").
        limit: Maximum number of recommendations to return (default: 10).

    Returns:
        A string listing recommended movies with their details.
    """
    print("in genre Function")
    print("#"*100)
    client = get_clickhouse_client()
    try:
        # How the query works:
        # * The INNER JOIN subquery counts credited actors per movie (cast_size, the popularity
        #   proxy). INNER (not LEFT) drops movies with no cast rows at all.
        # * `m.id IN (SELECT movie_id FROM genres ...)` keeps movies tagged with the requested genre.
        #   The comparison is case-insensitive so "sci-fi" matches "Sci-Fi". A movie has one genres
        #   row per genre, so IN (a semi-join) avoids the duplicates a JOIN on genres would create.
        # * `m.id NOT IN (...)` drops shorts/documentaries/adult titles, except when that excluded
        #   genre is the one requested (the `!=` clause), so asking for "Documentary" still works.
        # * `m.rank > 0` drops unrated movies (rank 0); `cast_size >= {min_cast}` drops obscure ones.
        # * Best rated first; cast size breaks ties so better-known films win.
        # {genre:String}, {min_cast:UInt32} and {limit:UInt32} are bound server-side (no injection).
        query = """
        SELECT m.name AS title, m.year AS year, round(toFloat64(m.rank), 1) AS rating
        FROM movies AS m
        INNER JOIN (
            SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
        ) AS c ON c.movie_id = m.id
        WHERE m.id IN (SELECT movie_id FROM genres WHERE lower(genre) = lower({genre:String}))
          AND m.id NOT IN (
              SELECT movie_id FROM genres
              WHERE genre IN ('Short', 'Documentary', 'Adult') AND lower(genre) != lower({genre:String})
          )
          AND m.rank > 0
          AND c.cast_size >= {min_cast:UInt32}
        ORDER BY m.rank DESC, c.cast_size DESC
        LIMIT {limit:UInt32}
        """
        print("Running query")
        print("#"*100)
        print(f"Query: {query}")
        print(f"Parameters: genre={genre}, limit={limit}")
        print("#"*100)
        # int(limit): the LLM sometimes sends numbers as floats/strings; UInt32 binding needs an int.
        result = client.query(query, parameters={"genre": genre, "min_cast": MIN_CAST_SIZE, "limit": int(limit)})

        print("Query Executed")
        print("#"*100)
        movies = []
        # named_results() yields one dict per row, keyed by the SELECT aliases.
        for record in result.named_results():
            # Convert "Matrix, The" -> "The Matrix"; fall back to placeholders for empty values.
            title = to_display_title(record["title"]) or "Unknown"
            year = record["year"] or "Unknown"
            rating = record["rating"] or "N/A"
            movie_info = f"- {title} ({year})"
            movie_info += f" [Rating: {rating}]"
            movies.append(movie_info)

        if movies:
            return f"Here are {len(movies)} {genre} movie recommendations:\n" + "\n".join(movies)
        else:
            return f"No movies found for genre: {genre}"
    except Exception as e:
        return f"Error querying movies: {str(e)}"
    finally:
        client.close()


def recommend_movies_by_actor(actor_name: str, limit: int = 10) -> str:
    """
    Recommends movies featuring a specific actor, feature films first, best rated first.

    Args:
        actor_name: The full name of the actor (e.g., "Tom Hanks").
        limit: Maximum number of recommendations to return (default: 10).

    Returns:
        A string listing movies featuring the actor.
    """
    client = get_clickhouse_client()
    try:
        # How the query works (an f-string: {PERSON_NAME} is spliced in, {{...}} stay parameters):
        # * Innermost IN: ids of every actor whose display name (suffix like "(I)" stripped) matches,
        #   case-insensitively. Namesakes all match, which is usually what the user means.
        # * Middle IN: ids of movies those actors appear in (roles is ~3.4M rows, so it is filtered
        #   by actor_id before anything is joined).
        # * LEFT JOIN cast-size subquery: kept LEFT so movies with incomplete cast data still show.
        # * ORDER BY: the boolean `cast_size >= feature_cast` sorts feature films (1) before shorts
        #   and TV episodes (0), then by rating, then newest first. Unrated movies are not removed
        #   here because a filmography should be complete.
        query = f"""
        SELECT m.name AS title, m.year AS year, round(toFloat64(m.rank), 1) AS rating
        FROM movies AS m
        LEFT JOIN (
            SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
        ) AS c ON c.movie_id = m.id
        WHERE m.id IN (
            SELECT movie_id FROM roles
            WHERE actor_id IN (
                SELECT id FROM actors WHERE lower({PERSON_NAME}) = lower({{actor_name:String}})
            )
        )
        ORDER BY c.cast_size >= {{feature_cast:UInt32}} DESC, m.rank DESC, m.year DESC
        LIMIT {{limit:UInt32}}
        """
        result = client.query(
            query, parameters={"actor_name": actor_name, "feature_cast": FEATURE_CAST_SIZE, "limit": int(limit)}
        )
        movies = []
        for record in result.named_results():
            title = to_display_title(record["title"]) or "Unknown"
            year = record["year"] or "Unknown"
            # rank 0 (unrated) rounds to 0.0, which is falsy, so it is shown as "N/A".
            rating = record["rating"] or "N/A"
            movies.append(f"- {title} ({year}) [Rating: {rating}]")

        if movies:
            return f"Movies featuring {actor_name}:\n" + "\n".join(movies)
        else:
            return f"No movies found for actor: {actor_name}"
    except Exception as e:
        return f"Error querying movies: {str(e)}"
    finally:
        client.close()


def recommend_movies_by_director(director_name: str, limit: int = 10) -> str:
    """
    Recommends movies directed by a specific director, feature films first, best rated first.

    Args:
        director_name: The full name of the director (e.g., "Steven Spielberg").
        limit: Maximum number of recommendations to return (default: 10).

    Returns:
        A string listing movies directed by the director.
    """
    client = get_clickhouse_client()
    try:
        # Same shape as recommend_movies_by_actor, but through the directors table and the
        # movie_directors link table (director_id -> movie_id) instead of actors/roles.
        # Cast size still comes from roles: it is only used to rank features before shorts.
        query = f"""
        SELECT m.name AS title, m.year AS year, round(toFloat64(m.rank), 1) AS rating
        FROM movies AS m
        LEFT JOIN (
            SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
        ) AS c ON c.movie_id = m.id
        WHERE m.id IN (
            SELECT movie_id FROM movie_directors
            WHERE director_id IN (
                SELECT id FROM directors WHERE lower({PERSON_NAME}) = lower({{director_name:String}})
            )
        )
        ORDER BY c.cast_size >= {{feature_cast:UInt32}} DESC, m.rank DESC, m.year DESC
        LIMIT {{limit:UInt32}}
        """
        result = client.query(
            query, parameters={"director_name": director_name, "feature_cast": FEATURE_CAST_SIZE, "limit": int(limit)}
        )
        movies = []
        for record in result.named_results():
            title = to_display_title(record["title"]) or "Unknown"
            year = record["year"] or "Unknown"
            rating = record["rating"] or "N/A"
            movies.append(f"- {title} ({year}) [Rating: {rating}]")

        if movies:
            return f"Movies directed by {director_name}:\n" + "\n".join(movies)
        else:
            return f"No movies found for director: {director_name}"
    except Exception as e:
        return f"Error querying movies: {str(e)}"
    finally:
        client.close()


def recommend_movies_by_year(year: int, limit: int = 10) -> str:
    """
    Recommends the best rated, well-known movies released in a specific year.

    Args:
        year: The release year (the IMDB dataset covers 1888-2008).
        limit: Maximum number of recommendations to return (default: 10).

    Returns:
        A string listing movies from that year.
    """
    client = get_clickhouse_client()
    try:
        # Like the genre query, minus the genre filter: movies from {year}, excluding every
        # Short/Documentary/Adult title (NOT IN subquery on genres), rated (rank > 0) and with a
        # cast of at least MIN_CAST_SIZE (INNER JOIN on the per-movie cast-size subquery).
        # The year is not selected because every row shares it.
        query = """
        SELECT m.name AS title, round(toFloat64(m.rank), 1) AS rating
        FROM movies AS m
        INNER JOIN (
            SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
        ) AS c ON c.movie_id = m.id
        WHERE m.year = {year:UInt32}
          AND m.id NOT IN (SELECT movie_id FROM genres WHERE genre IN ('Short', 'Documentary', 'Adult'))
          AND m.rank > 0
          AND c.cast_size >= {min_cast:UInt32}
        ORDER BY m.rank DESC, c.cast_size DESC
        LIMIT {limit:UInt32}
        """
        result = client.query(query, parameters={"year": int(year), "min_cast": MIN_CAST_SIZE, "limit": int(limit)})
        movies = []
        for record in result.named_results():
            title = to_display_title(record["title"]) or "Unknown"
            rating = record["rating"] or "N/A"
            movie_info = f"- {title}"
            movie_info += f" [Rating: {rating}]"
            movies.append(movie_info)

        if movies:
            return f"Top movies from {year}:\n" + "\n".join(movies)
        else:
            return f"No movies found for year: {year}"
    except Exception as e:
        return f"Error querying movies: {str(e)}"
    finally:
        client.close()


def search_movies_by_keyword(keyword: str, limit: int = 10) -> str:
    """
    Searches for movies whose title contains a keyword (case-insensitive), best-known first.

    Args:
        keyword: The keyword to search for in movie titles.
        limit: Maximum number of results to return (default: 10).

    Returns:
        A string listing matching movies.
    """
    client = get_clickhouse_client()
    try:
        # positionCaseInsensitiveUTF8(haystack, needle) returns the 1-based match position, or 0
        # when the keyword is absent, so `> 0` is a case-insensitive "contains" test. It searches
        # the stored form ("Matrix, The"), which is fine for a substring match.
        # Sorted by cast size first: for a keyword search the user usually means the famous film
        # (e.g. "matrix" -> The Matrix, not an obscure short), and rating second.
        query = """
        SELECT m.name AS title, m.year AS year, round(toFloat64(m.rank), 1) AS rating
        FROM movies AS m
        LEFT JOIN (
            SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
        ) AS c ON c.movie_id = m.id
        WHERE positionCaseInsensitiveUTF8(m.name, {keyword:String}) > 0
        ORDER BY c.cast_size DESC, m.rank DESC
        LIMIT {limit:UInt32}
        """
        result = client.query(query, parameters={"keyword": keyword, "limit": int(limit)})
        movies = []
        for record in result.named_results():
            title = to_display_title(record["title"]) or "Unknown"
            year = record["year"] or "Unknown"
            rating = record["rating"] or "N/A"
            movie_info = f"- {title} ({year})"
            movie_info += f" [Rating: {rating}]"
            movies.append(movie_info)

        if movies:
            return f"Movies matching '{keyword}':\n" + "\n".join(movies)
        else:
            return f"No movies found matching: {keyword}"
    except Exception as e:
        return f"Error querying movies: {str(e)}"
    finally:
        client.close()


def get_movie_details(movie_title: str) -> str:
    """
    Gets detailed information about a specific movie: year, rating, genres, directors and top cast.

    Args:
        movie_title: The title of the movie (e.g., "The Godfather").

    Returns:
        A string with detailed movie information.
    """
    client = get_clickhouse_client()
    try:
        # Step 1: find the movie. The title is matched case-insensitively in both forms, as typed
        # ("The Godfather") and in IMDB's inverted form ("Godfather, The").
        # Several movies can share a title (e.g. two "Godfather, The"); pick the one with the largest cast.
        query = """
        SELECT m.id AS id, m.name AS title, m.year AS year, round(toFloat64(m.rank), 1) AS rating
        FROM movies AS m
        LEFT JOIN (
            SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
        ) AS c ON c.movie_id = m.id
        WHERE lower(m.name) IN (lower({movie_title:String}), lower({imdb_title:String}))
        ORDER BY c.cast_size DESC, m.rank DESC
        LIMIT 1
        """
        result = client.query(
            query, parameters={"movie_title": movie_title, "imdb_title": to_imdb_title(movie_title)}
        )
        rows = list(result.named_results())
        record = rows[0] if rows else None

        if not record:
            return f"Movie not found: {movie_title}"

        # Step 2: three small follow-up queries keyed by the movie id, reusing the same client.
        params = {"movie_id": record["id"]}
        # Genres: groupUniqArray collects the distinct genres into one array; arraySort orders it.
        # The query always returns exactly one row (an empty array if the movie has no genres).
        genres = client.query(
            "SELECT arraySort(groupUniqArray(genre)) FROM genres WHERE movie_id = {movie_id:UInt32}",
            parameters=params,
        ).result_rows[0][0]
        # Directors: the IN subquery resolves director ids through the movie_directors link table,
        # so the directors table is filtered by id instead of joined. DISTINCT removes duplicates.
        directors = [row[0] for row in client.query(
            f"SELECT DISTINCT {PERSON_NAME} AS name FROM directors "
            "WHERE id IN (SELECT director_id FROM movie_directors WHERE movie_id = {movie_id:UInt64}) "
            "ORDER BY name",
            parameters=params,
        ).result_rows]
        # The dataset has no billing order, so list the cast members with the most film credits first.
        # The inner subquery narrows actors to this movie's cast (via an IN on roles) before the join,
        # then the join back to the full roles table counts each cast member's career credits.
        actors = [row[0] for row in client.query(
            f"""
            SELECT {PERSON_NAME} AS name, count() AS films
            FROM roles AS r
            INNER JOIN (
                SELECT id, first_name, last_name FROM actors
                WHERE id IN (SELECT actor_id FROM roles WHERE movie_id = {{movie_id:UInt32}})
            ) AS a ON a.id = r.actor_id
            GROUP BY name
            ORDER BY films DESC
            LIMIT 10
            """,
            parameters=params,
        ).result_rows]

        # Step 3: format as "Field: value" lines, skipping empty fields (e.g. rating 0 = unrated).
        details = [f"Title: {to_display_title(record['title']) or 'Unknown'}"]
        if record['year']:
            details.append(f"Year: {record['year']}")
        if record['rating']:
            details.append(f"Rating: {record['rating']}")
        if genres:
            details.append(f"Genres: {', '.join(genres)}")
        if directors:
            details.append(f"Directors: {', '.join(directors)}")
        if actors:
            details.append(f"Actors: {', '.join(actors)}")  # Top 10 by number of film credits

        return "\n".join(details)
    except Exception as e:
        return f"Error querying movie details: {str(e)}"
    finally:
        client.close()


def create_agent() -> LlmAgent:
    """Constructs the ADK agent for movie recommendations.

    Returns an ``LlmAgent`` that pairs the Gemini model ``MOVIE_AGENT_MODEL`` with the six tools
    above. ``instruction`` is the system prompt: it describes the dataset (year range, genre names)
    and tells the model which tool to use for which kind of request. ADK builds each tool's schema
    from its signature and docstring and runs the Python function when Gemini calls it.
    """

    # client = get_clickhouse_client()
    # (Left commented out on purpose: the tools open their own clients, so no connection is made
    # at startup and the server starts even if ClickHouse is briefly unavailable.)

    return LlmAgent(
        model=MOVIE_AGENT_MODEL,
        # Agent names must be valid identifiers in ADK (no spaces), hence "Movie_Agent".
        name="Movie_Agent",
        # The system prompt. Listing the exact genre names lets the model map "science fiction" to
        # "Sci-Fi" before calling recommend_movies_by_genre.
        instruction="""
            **Role:** You are a movie recommendation assistant that helps users discover movies
            from the IMDB dataset stored in ClickHouse. Your goal is to provide personalized movie
            recommendations based on user preferences.

            **About the data:** The dataset covers movies released between 1888 and 2008. Ratings are
            IMDb ratings out of 10. Available genres are: Action, Adult, Adventure, Animation, Comedy,
            Crime, Documentary, Drama, Family, Fantasy, Film-Noir, Horror, Music, Musical, Mystery,
            Romance, Sci-Fi, Short, Thriller, War, Western. Map user wording to these names
            (e.g. "science fiction" -> "Sci-Fi").

            **Core Directives:**

            *   **Genre Recommendations:** Use `recommend_movies_by_genre` to find movies in a
                    specific genre (e.g., Action, Comedy, Drama, Sci-Fi, Horror).
            *   **Actor-based Recommendations:** Use `recommend_movies_by_actor` to find movies
                    featuring a specific actor.
            *   **Director-based Recommendations:** Use `recommend_movies_by_director` to find
                    movies directed by a specific director.
            *   **Year-based Recommendations:** Use `recommend_movies_by_year` to find movies
                    from a specific release year.
            *   **Keyword Search:** Use `search_movies_by_keyword` to search for movies by title
                    keywords.
            *   **Movie Details:** Use `get_movie_details` to get comprehensive information about
                    a specific movie.
            *   **Be Helpful and Conversational:** Engage naturally with users, ask clarifying
                    questions if needed, and provide thoughtful recommendations.
            *   **Combine Criteria:** If users provide multiple criteria (e.g., genre and year),
                    use multiple tools to provide comprehensive recommendations.
        """,
        # Plain Python functions: ADK wraps each one in a FunctionTool automatically.
        tools=[
            recommend_movies_by_genre,
            recommend_movies_by_actor,
            recommend_movies_by_director,
            recommend_movies_by_year,
            search_movies_by_keyword,
            get_movie_details,
        ],
    )
