# Movie Agent and ClickHouse

The Movie Agent (`Solution/movie_agent/`) is a Google ADK `LlmAgent` with six Python tools. Each tool runs parameterised SQL against the IMDB dataset in ClickHouse through [`clickhouse-connect`](https://clickhouse.com/docs/integrations/python) and returns plain text for the LLM.

- [Files](#files)
- [The IMDB dataset](#the-imdb-dataset)
- [Connecting to ClickHouse](#connecting-to-clickhouse)
- [Helpers: titles and names](#helpers-titles-and-names)
- [Ranking strategy](#ranking-strategy)
- [The six tools](#the-six-tools)
- [Parameter binding](#parameter-binding)
- [Performance notes](#performance-notes)
- [Using your own ClickHouse](#using-your-own-clickhouse)
- [From Neo4j to ClickHouse](#from-neo4j-to-clickhouse)

---

## Files

| File | Contents |
|------|----------|
| `Solution/movie_agent/agent.py` | Configuration constants, `get_clickhouse_client()`, title/name helpers, the six tools, `create_agent()` |
| `Solution/movie_agent/agent_executor.py` | `MovieAgentExecutor` (A2A <-> ADK bridge) and the A2A/GenAI part converters |
| `Solution/movie_agent/__main__.py` | Agent card, ADK `Runner`, A2A server on `localhost:10005` |
| `clickhouse/schema.sql` | Table definitions (commented per column) |
| `clickhouse/load_imdb.py` | Creates the schema and loads the data from S3 into your own server |
| `tests/check_movie_tools.py` | Calls all six tools directly, no LLM involved |

`create_agent()` returns:

```python
LlmAgent(
    model=MOVIE_AGENT_MODEL,          # MOVIE_AGENT_MODEL or GEMINI_MODEL or "gemini-3.8-flash"
    name="Movie_Agent",
    instruction="""...role, the data (1888-2008, ratings out of 10, the 21 genre names,
                   map "science fiction" -> "Sci-Fi"), when to use each tool...""",
    tools=[recommend_movies_by_genre, recommend_movies_by_actor, recommend_movies_by_director,
           recommend_movies_by_year, search_movies_by_keyword, get_movie_details],
)
```

ADK builds each tool's function declaration from the Python signature and docstring, so the `Args:` sections in the docstrings are what Gemini sees as parameter descriptions.

---

## The IMDB dataset

The data is the [IMDB example dataset](https://clickhouse.com/docs/getting-started/example-datasets/imdb) from the ClickHouse docs (the "IJS" IMDB snapshot). It has 6 tables:

| Table | Rows (approx.) | Columns | Notes |
|-------|----------------|---------|-------|
| `movies` | 388k | `id UInt32`, `name String`, `year UInt32`, `rank Float32` | `rank` = IMDb rating out of 10, **0 = unrated** |
| `actors` | 818k | `id UInt32`, `first_name String`, `last_name String`, `gender FixedString(1)` | |
| `directors` | 87k | `id UInt32`, `first_name String`, `last_name String` | |
| `roles` | 3.4M | `actor_id UInt32`, `movie_id UInt32`, `role String`, `created_at DateTime` | Actor <-> movie link; `created_at` is filled at load time |
| `movie_directors` | 371k | `director_id UInt32`, `movie_id UInt64` | Director <-> movie link. Note the `UInt64` |
| `genres` | 395k | `movie_id UInt32`, `genre String` | One row per (movie, genre) |

```
actors 1───* roles *───1 movies 1───* genres
                          1
                          │
                          *
                   movie_directors *───1 directors
```

### Quirks you need to know

| Quirk | Example | How the agent deals with it |
|-------|---------|-----------------------------|
| **Leading articles are moved to the end of titles** | "The Matrix" is stored as `Matrix, The`; "A Few Good Men" as `Few Good Men, A` | `to_display_title()` converts results back; `get_movie_details` also searches for `to_imdb_title(title)` |
| **Namesake suffixes in first names** | `Chris (I)`, `Chris (II)` | The SQL expression `PERSON_NAME` strips everything from ` (` on, both for matching and display |
| **`rank = 0` means unrated** | Only about 67k of the 388k movies have a rating | Genre and year recommendations require `m.rank > 0` |
| **Coverage ends in 2008** | Movies from 1888 to 2008 | Stated in the agent instruction and in the `recommend_movies_by_year` docstring |
| **No vote counts and no billing order** | A 9.8-rated short with 2 credited actors would top a naive ranking | Cast size is used as a popularity proxy (see [Ranking strategy](#ranking-strategy)) |
| **Duplicate titles** | Several movies called `Godfather, The` | `get_movie_details` picks the match with the largest cast |
| **Genre list includes non-feature categories** | `Short`, `Documentary`, `Adult` | Excluded from genre/year recommendations unless asked for |

Genres present in the data (as listed in the agent instruction): Action, Adult, Adventure, Animation, Comedy, Crime, Documentary, Drama, Family, Fantasy, Film-Noir, Horror, Music, Musical, Mystery, Romance, Sci-Fi, Short, Thriller, War, Western.

---

## Connecting to ClickHouse

Configuration is read once at import time from environment variables (see [CONFIGURATION.md](CONFIGURATION.md)):

```python
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "localhost")
CLICKHOUSE_SECURE = os.getenv("CLICKHOUSE_SECURE", "false").strip().lower() in ("1", "true", "yes")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT") or (8443 if CLICKHOUSE_SECURE else 8123))
```

`get_clickhouse_client()` creates a client with `clickhouse_connect.get_client(host, port, username, password, database, secure)`, calls `client.ping()`, and raises `ConnectionError` (after closing the client) if the server does not answer. **Every tool call creates its own client** and closes it in a `finally:` block. clickhouse-connect talks to ClickHouse's HTTP(S) interface, which is why the ports are 8123 / 8443 / 443 rather than the native-protocol port 9000.

All tools catch exceptions and **return** an error string (`Error querying movies: ...` or `Error querying movie details: ...`) instead of raising, so a database problem shows up in the LLM's answer rather than failing the A2A task. Note that the connection itself is made before the `try:` in each tool, so a failed ping does propagate as an exception.

---

## Helpers: titles and names

```python
ARTICLES = ("The", "A", "An")

to_display_title("Matrix, The")  # -> "The Matrix"
to_imdb_title("The Matrix")      # -> "Matrix, The"   (prefix match is case-insensitive)

PERSON_NAME = "concat(splitByString(' (', first_name)[1], ' ', last_name)"
# first_name "Chris (I)", last_name "Evans"  ->  "Chris Evans"
```

`PERSON_NAME` is a SQL **expression** that is pasted into queries with Python f-strings. It is a constant, never user input. Name matching uses `lower(PERSON_NAME) = lower({name:String})`, i.e. an exact, case-insensitive match on the full name: "Hanks" alone will not match "Tom Hanks".

---

## Ranking strategy

The dataset has ratings but no vote counts, so ordering by `rank` alone surfaces obscure titles with a handful of votes. The agent uses the **number of credited actors** (rows in `roles` for the movie, "cast size") as a stand-in for how big and well-known a production is.

The cast sizes are computed with the same derived table in every tool:

```sql
SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
```

| Constant | Value | Source | Used by | Effect |
|----------|-------|--------|---------|--------|
| `MIN_CAST_SIZE` | 30 | env `MOVIE_MIN_CAST_SIZE` | genre, year | Only movies with at least this many credited actors are recommended |
| `FEATURE_CAST_SIZE` | 10 | constant in `agent.py` | actor, director | Movies with at least 10 actors (feature films) are listed before smaller titles (shorts, TV) |
| excluded genres | `Short`, `Documentary`, `Adult` | hard-coded in SQL | genre, year | Removed unless the requested genre is one of them (genre tool only) |
| `m.rank > 0` | | hard-coded in SQL | genre, year | Unrated movies are not recommended |

Summary per tool:

| Tool | Filters | ORDER BY |
|------|---------|----------|
| `recommend_movies_by_genre` | genre match, not Short/Documentary/Adult (unless requested), rated, cast >= `MIN_CAST_SIZE` | `rank DESC, cast_size DESC` |
| `recommend_movies_by_actor` | actor's movies | `cast_size >= 10 DESC, rank DESC, year DESC` |
| `recommend_movies_by_director` | director's movies | `cast_size >= 10 DESC, rank DESC, year DESC` |
| `recommend_movies_by_year` | year, not Short/Documentary/Adult, rated, cast >= `MIN_CAST_SIZE` | `rank DESC, cast_size DESC` |
| `search_movies_by_keyword` | title contains keyword | `cast_size DESC, rank DESC` |
| `get_movie_details` | title or IMDB-form title | `cast_size DESC, rank DESC`, `LIMIT 1` |

Raising `MOVIE_MIN_CAST_SIZE` gives fewer, more mainstream results; lowering it lets smaller, older films in.

---

## The six tools

All tools take a `limit` (default 10, cast with `int()` because the LLM may send it as a float or string), except `get_movie_details`. Ratings are returned as `round(toFloat64(m.rank), 1)`: the `Float32` column is widened to `Float64` before rounding, so the value comes back as a clean one-decimal number. Each result row is formatted as `- <Display Title> (<year>) [Rating: <rating>]`, with `N/A` for a 0 rating. Real output from `tests/check_movie_tools.py` against the playground:

```
Here are 5 Sci-Fi movie recommendations:
- Star Wars (1977) [Rating: 8.8]
- Star Wars: Episode V - The Empire Strikes Back (1980) [Rating: 8.7]
- Kid senshi Gandamu III: Meguriai sorahen (1982) [Rating: 8.7]
- Octopus (1998) [Rating: 8.6]
- The Matrix (1999) [Rating: 8.5]
```

### `recommend_movies_by_genre(genre, limit=10)`

```sql
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
```

- The `INNER JOIN` on cast sizes drops movies with no credited actors and makes `cast_size` available for filtering.
- The genre filter is an `IN` subquery (case-insensitive), so a movie with several genres appears once.
- The `NOT IN` removes shorts, documentaries and adult titles, but the `lower(genre) != lower({genre})` clause keeps them when they are the requested genre ("Recommend some documentaries" works).
- This function also prints the query and parameters to the console for debugging.

### `recommend_movies_by_actor(actor_name, limit=10)`

```sql
SELECT m.name AS title, m.year AS year, round(toFloat64(m.rank), 1) AS rating
FROM movies AS m
LEFT JOIN (
    SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
) AS c ON c.movie_id = m.id
WHERE m.id IN (
    SELECT movie_id FROM roles
    WHERE actor_id IN (
        SELECT id FROM actors
        WHERE lower(concat(splitByString(' (', first_name)[1], ' ', last_name)) = lower({actor_name:String})
    )
)
ORDER BY c.cast_size >= {feature_cast:UInt32} DESC, m.rank DESC, m.year DESC
LIMIT {limit:UInt32}
```

- Nested `IN` subqueries walk actors -> roles -> movies (the SQL form of a graph traversal). If several actors share the stripped name (e.g. two "Chris Evans"), their movies are combined.
- `LEFT JOIN` keeps movies without cast rows; ClickHouse fills the missing `cast_size` with 0 by default.
- `ORDER BY <boolean> DESC` puts feature films (cast >= 10) first, then by rating, then newest first. Unrated movies are not excluded here.

### `recommend_movies_by_director(director_name, limit=10)`

Same shape as the actor tool, walking directors -> `movie_directors` -> movies:

```sql
WHERE m.id IN (
    SELECT movie_id FROM movie_directors
    WHERE director_id IN (
        SELECT id FROM directors WHERE lower(<PERSON_NAME>) = lower({director_name:String})
    )
)
ORDER BY c.cast_size >= {feature_cast:UInt32} DESC, m.rank DESC, m.year DESC
```

### `recommend_movies_by_year(year, limit=10)`

```sql
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
```

The year is not repeated in each output line (`- <title> [Rating: x]`); the header says `Top movies from <year>:`.

### `search_movies_by_keyword(keyword, limit=10)`

```sql
SELECT m.name AS title, m.year AS year, round(toFloat64(m.rank), 1) AS rating
FROM movies AS m
LEFT JOIN (
    SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id
) AS c ON c.movie_id = m.id
WHERE positionCaseInsensitiveUTF8(m.name, {keyword:String}) > 0
ORDER BY c.cast_size DESC, m.rank DESC
LIMIT {limit:UInt32}
```

- `positionCaseInsensitiveUTF8(haystack, needle) > 0` is a case-insensitive substring test (Cypher's `toLower(x) CONTAINS toLower(y)`).
- It matches the **stored** title, so a keyword with a leading article does not match: `"matrix"` finds `Matrix, The` but `"The Matrix"` does not.
- Ordered by cast size first, so the big productions come before obscure titles that happen to share the word.

### `get_movie_details(movie_title)`

Runs four queries on one client:

1. **Find the movie**: match the title as given *or* in IMDB form, pick the biggest cast:

   ```sql
   SELECT m.id AS id, m.name AS title, m.year AS year, round(toFloat64(m.rank), 1) AS rating
   FROM movies AS m
   LEFT JOIN (SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id) AS c ON c.movie_id = m.id
   WHERE lower(m.name) IN (lower({movie_title:String}), lower({imdb_title:String}))
   ORDER BY c.cast_size DESC, m.rank DESC
   LIMIT 1
   ```

   with `imdb_title = to_imdb_title(movie_title)`. Returns `Movie not found: ...` when nothing matches.
2. **Genres**: `SELECT arraySort(groupUniqArray(genre)) FROM genres WHERE movie_id = {movie_id:UInt32}`, one sorted array of distinct genres (Cypher's `collect(DISTINCT g.name)`).
3. **Directors**: `SELECT DISTINCT <PERSON_NAME> AS name FROM directors WHERE id IN (SELECT director_id FROM movie_directors WHERE movie_id = {movie_id:UInt64}) ORDER BY name`. The parameter is typed `UInt64` because `movie_directors.movie_id` is `UInt64`.
4. **Top 10 actors**: the dataset has no billing order, so the cast is ranked by each actor's total number of film credits:

   ```sql
   SELECT <PERSON_NAME> AS name, count() AS films
   FROM roles AS r
   INNER JOIN (
       SELECT id, first_name, last_name FROM actors
       WHERE id IN (SELECT actor_id FROM roles WHERE movie_id = {movie_id:UInt32})
   ) AS a ON a.id = r.actor_id
   GROUP BY name
   ORDER BY films DESC
   LIMIT 10
   ```

   The inner subquery shrinks `actors` to this movie's cast before the join, so the join's in-memory hash table stays small.

Output:

```
Title: The Godfather
Year: 1972
Rating: 9.0
Genres: Crime, Drama
Directors: Francis Ford Coppola
Actors: ...
```

(Lines are omitted when the value is empty or 0.)

---

## Parameter binding

Values coming from the LLM are **never** formatted into SQL strings. The queries use ClickHouse server-side parameters, `{name:Type}`, and pass values separately:

```python
client.query(query, parameters={"genre": genre, "min_cast": MIN_CAST_SIZE, "limit": int(limit)})
```

clickhouse-connect sends the parameters alongside the query and ClickHouse substitutes them with the declared type (`String`, `UInt32`, `UInt64`), so a genre like `Sci-Fi' OR 1=1 --` is just a string that matches nothing.

Some queries are Python f-strings because they splice in the `PERSON_NAME` constant. In those, the ClickHouse placeholders are written with doubled braces, `{{actor_name:String}}`, so that the f-string emits a literal `{actor_name:String}`.

Rule of thumb when you edit or add queries: f-string interpolation only for trusted constants, `{name:Type}` for every value.

---

## Performance notes

- Against the public playground, each tool call takes roughly **2 seconds** (`tests/check_movie_tools.py` prints the time per tool). A full chat turn adds the LLM round-trips on top.
- Each call opens a new HTTPS client and pings the server before querying; this keeps the tools stateless and simple at the cost of a connection setup per call.
- The main per-query cost is the cast-size aggregate (`count()` over all ~3.4M `roles` rows, grouped by `movie_id`). It is recomputed in every query. If you run your own server and want faster responses, a materialized table of cast sizes would remove it, but the code does not do this.
- `IN (subquery)` is used for the relationship hops. ClickHouse evaluates the subquery once into a set and filters the outer table with it, which avoids building large join hash tables; the `roles` and `movie_directors` sorting keys (`actor_id, ...` and `director_id, ...`) make "all movies of person X" a range read.
- `get_movie_details` makes four round-trips; the others make one.
- The playground is shared and may rate-limit or be slower at busy times.

---

## Using your own ClickHouse

The public playground (`sql-clickhouse.clickhouse.com:443`, user `demo`, empty password, database `imdb`) needs no setup but is read-only and shared. To use your own server:

1. Edit `.env` (start from `.env.example`): comment out the playground block and fill in your own. For ClickHouse Cloud: host from the console's *Connect* dialog, port `8443`, user `default`, your password, `CLICKHOUSE_SECURE=true`. For a local server: `CLICKHOUSE_HOST=localhost`, port `8123`, `CLICKHOUSE_SECURE=false`. See [CONFIGURATION.md](CONFIGURATION.md#switching-clickhouse-targets).
2. Load the data:

   ```powershell
   python clickhouse/load_imdb.py
   ```

   The script connects **without** a database (it may not exist yet), runs every statement in `clickhouse/schema.sql` (with `imdb` renamed to `CLICKHOUSE_DATABASE`), then for each table runs `TRUNCATE` and

   ```sql
   INSERT INTO `<db>`.<table> (<cols>)
   SELECT <cols> FROM s3('https://datasets-documentation.s3.eu-west-3.amazonaws.com/imdb/<file>.tsv.gz', 'TSVWithNames')
   ```

   The ClickHouse **server** downloads the files from S3, so it needs outbound internet access. The script is safe to re-run. The client timeout is 900 s per statement.
3. Check it: `python tests/check_movie_tools.py` should print `6/6 checks passed`.

---

## From Neo4j to ClickHouse

This project started as a Neo4j version in which movies, people and genres were nodes and the links were relationships. The port replaced Cypher graph patterns with SQL over link tables: a relationship hop becomes a `JOIN` or, more often here, an `IN (subquery)`. The Neo4j code is not in this repo; the left column shows the kind of Cypher pattern each SQL construct replaces.

| Graph concept / Cypher pattern | ClickHouse SQL in this project |
|--------------------------------|--------------------------------|
| `(:Movie)` node with properties | Row in `movies` |
| `(:Person)` node | Row in `actors` / `directors` |
| `(m:Movie)-[:IN_GENRE]->(g:Genre {name: $genre})` | `m.id IN (SELECT movie_id FROM genres WHERE lower(genre) = lower({genre:String}))` |
| `(a:Person {name: $name})-[:ACTED_IN]->(m:Movie)` | `m.id IN (SELECT movie_id FROM roles WHERE actor_id IN (SELECT id FROM actors WHERE lower(PERSON_NAME) = lower({actor_name:String})))` |
| `(d:Person)-[:DIRECTED]->(m:Movie)` | the same through `movie_directors` / `directors` |
| `NOT (m)-[:IN_GENRE]->(:Genre {name: 'Short'})` | `m.id NOT IN (SELECT movie_id FROM genres WHERE genre IN ('Short', ...))` |
| `size((m)<-[:ACTED_IN]-())` / `COUNT { ... }` (degree) | `SELECT movie_id, count() AS cast_size FROM roles GROUP BY movie_id`, joined back |
| `collect(DISTINCT g.name)` | `groupUniqArray(genre)` (sorted with `arraySort`) |
| `collect(...)` | `groupArray(...)` |
| `toLower(m.title) CONTAINS toLower($kw)` | `positionCaseInsensitiveUTF8(m.name, {keyword:String}) > 0` |
| `m.imdbRating` | `movies.rank` (0 = unrated) |
| `$param` query parameters | `{param:Type}` server-side parameters |
| `ORDER BY ... LIMIT $n` | `ORDER BY ... LIMIT {limit:UInt32}` |
| `MATCH (n) RETURN n LIMIT 25` | `SELECT count() FROM <table>` per table, then `SELECT * FROM movies LIMIT 25` (notebook) |
| `CALL apoc.meta.nodeTypeProperties()` | `DESCRIBE TABLE ...` / `SELECT ... FROM system.columns` (notebook) |
| `(:User)-[:RATED {rating}]->(:Movie)` | No equivalent: the IMDB dataset has only the aggregate `movies.rank` |
| neo4j driver `session.run(query, params)` + `record["x"]` | `client.query(query, parameters=...)` + `result.named_results()` (dicts keyed by column alias) |

Why `IN` subqueries rather than chains of `JOIN`s: in ClickHouse the right side of a `JOIN` is loaded into an in-memory hash table, so joining the full `actors` (818k) or `roles` (3.4M) tables for every request is wasteful. Filtering with `IN` first reduces each hop to a set of ids. Where a join is needed for output columns (cast sizes, actor names in `get_movie_details`), the right side is pre-aggregated or pre-filtered.

The notebooks in `Notebook/` show more Cypher-to-SQL ports (most active actors, director filmographies, rating distribution, duplicate titles); see [DEVELOPMENT.md](DEVELOPMENT.md#notebooks).
