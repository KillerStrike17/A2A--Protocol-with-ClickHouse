-- ClickHouse schema for the movie recommendation agent.
-- This is the IMDB example dataset from the ClickHouse docs
-- (https://clickhouse.com/docs/getting-started/example-datasets/imdb):
--   movies           -> id, name, year, rank (IMDb rating, 0 = unrated)
--   actors           -> people who acted in movies
--   directors        -> people who directed movies
--   roles            -> actor  <-> movie (with role name)
--   movie_directors  -> director <-> movie
--   genres           -> movie  <-> genre
--
-- Titles keep IMDB's convention of moving a leading article to the end,
-- e.g. "Matrix, The" or "Few Good Men, A".
--
-- Run `python clickhouse/load_imdb.py` to create this schema in the database
-- named by CLICKHOUSE_DATABASE (default `imdb`) and load the data from S3.
--
-- Notes for editing this file:
--   * load_imdb.py drops every line that starts with "--" (after leading
--     whitespace) and then splits the rest on semicolons, so keep comments on
--     their own lines and never put a semicolon inside SQL text.
--   * load_imdb.py rewrites "imdb.<table>" and the CREATE DATABASE line to the
--     configured database name, so keep the "imdb." prefix on every table.
--   * The public SQL playground (sql-clickhouse.clickhouse.com) already has
--     these tables in its read-only `imdb` database. This file is only needed
--     for your own ClickHouse server.
--
-- Relationships (there are no enforced foreign keys in ClickHouse):
--   roles.actor_id             -> actors.id
--   roles.movie_id             -> movies.id
--   movie_directors.director_id -> directors.id
--   movie_directors.movie_id   -> movies.id
--   genres.movie_id            -> movies.id
--
-- Approximate row counts after loading: movies ~388k, actors ~818k,
-- directors ~87k, roles ~3.4M, movie_directors ~371k, genres ~395k.

CREATE DATABASE IF NOT EXISTS imdb;

-- One row per actor (or actress).
CREATE TABLE IF NOT EXISTS imdb.actors
(
    -- Actor id, referenced by roles.actor_id.
    id         UInt32,
    -- Given name. Namesakes are disambiguated with a Roman-numeral suffix,
    -- e.g. "Chris (I)". The Movie Agent strips the " (..." part for matching
    -- and display (see PERSON_NAME in Solution/movie_agent/agent.py).
    first_name String,
    -- Family name.
    last_name  String,
    -- Gender code, a single character.
    gender     FixedString(1)
)
ENGINE = MergeTree
-- Sorting key: rows are stored ordered by id, so lookups by id are cheap.
ORDER BY (id, first_name, last_name, gender);

-- One row per director.
CREATE TABLE IF NOT EXISTS imdb.directors
(
    -- Director id, referenced by movie_directors.director_id.
    id         UInt32,
    -- Given name, with the same "(I)"-style namesake suffixes as actors.
    first_name String,
    -- Family name.
    last_name  String
)
ENGINE = MergeTree
ORDER BY (id, first_name, last_name);

-- Movie <-> genre link table. A movie can have several genres (one row each).
CREATE TABLE IF NOT EXISTS imdb.genres
(
    -- References movies.id.
    movie_id UInt32,
    -- Genre name, e.g. "Action", "Sci-Fi", "Film-Noir". Also contains
    -- "Short", "Documentary" and "Adult", which the Movie Agent excludes from
    -- genre/year recommendations unless that genre is asked for.
    genre    String
)
ENGINE = MergeTree
ORDER BY (movie_id, genre);

-- Director <-> movie link table (a movie can have several directors).
CREATE TABLE IF NOT EXISTS imdb.movie_directors
(
    -- References directors.id.
    director_id UInt32,
    -- References movies.id. Note: UInt64 here but UInt32 in every other
    -- table, so queries that bind a movie id against this column use
    -- {movie_id:UInt64}.
    movie_id    UInt64
)
ENGINE = MergeTree
-- Sorted by director first: "all movies of director X" reads a contiguous range.
ORDER BY (director_id, movie_id);

-- One row per title (feature films, shorts, etc.), released 1888-2008.
CREATE TABLE IF NOT EXISTS imdb.movies
(
    -- Movie id, referenced by roles, genres and movie_directors.
    id   UInt32,
    -- Title with IMDB's trailing-article convention ("Matrix, The").
    -- Several movies can share a title.
    name String,
    -- Release year.
    year UInt32,
    -- IMDb user rating out of 10. 0 means unrated (only ~67k movies have a
    -- rating). There are no vote counts, so the agent uses cast size
    -- (number of roles rows) as a popularity proxy.
    rank Float32 DEFAULT 0
)
ENGINE = MergeTree
ORDER BY (id, name, year);

-- Actor <-> movie link table: one row per credited role.
-- count() of rows per movie_id is the "cast size" used for ranking.
CREATE TABLE IF NOT EXISTS imdb.roles
(
    -- References actors.id.
    actor_id   UInt32,
    -- References movies.id.
    movie_id   UInt32,
    -- Character name as credited.
    role       String,
    -- Load timestamp. Not part of the source files: filled with now() on insert.
    created_at DateTime DEFAULT now()
)
ENGINE = MergeTree
-- Sorted by actor first: "all movies of actor X" reads a contiguous range.
ORDER BY (actor_id, movie_id);
