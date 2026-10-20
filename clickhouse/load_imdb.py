"""
Create the ClickHouse schema and load the IMDB example dataset.

The dataset (~388k movies, ~818k actors, ~3.4M roles) is read directly by the
ClickHouse server from the public ClickHouse datasets bucket on S3, so the
server needs outbound internet access (ClickHouse Cloud has it by default).

Only needed for your own ClickHouse server (ClickHouse Cloud or local). The
default configuration in .env.example points at the public SQL playground,
which already has the `imdb` database and is read-only, so do not run this
script against the playground.

Usage:
    python clickhouse/load_imdb.py

Connection settings are read from the environment (or a .env file):
    CLICKHOUSE_HOST      (default: localhost)
    CLICKHOUSE_PORT      (default: 8123, or 8443 when CLICKHOUSE_SECURE=true)
    CLICKHOUSE_USERNAME  (default: default)
    CLICKHOUSE_PASSWORD  (default: "")
    CLICKHOUSE_DATABASE  (default: imdb)
    CLICKHOUSE_SECURE    (default: false; set "true" for ClickHouse Cloud)

The script is idempotent: tables are created if missing and truncated before loading.

How it works:
    1. schema_statements() reads clickhouse/schema.sql, drops the `--` comment
       lines, renames the `imdb` database to CLICKHOUSE_DATABASE and splits the
       file into individual statements, which are run with client.command().
    2. For each table in SOURCES it runs TRUNCATE, then a server-side
       ``INSERT INTO <db>.<table> (cols) SELECT cols FROM s3('<url>', 'TSVWithNames')``.
       The data never passes through this Python process: the ClickHouse
       server downloads and parses the gzipped TSV files itself.
    3. It prints the row count and elapsed time for each table.
"""
import os
import re
import time
from pathlib import Path

import clickhouse_connect
from dotenv import load_dotenv

# Load .env (searched for upwards from this file) so the same settings as the agents are used.
load_dotenv()

# Same variable names, defaults and parsing as Solution/movie_agent/agent.py.
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "localhost")
# Any of "1", "true", "yes" (case-insensitive) enables TLS.
CLICKHOUSE_SECURE = os.getenv("CLICKHOUSE_SECURE", "false").strip().lower() in ("1", "true", "yes")
# HTTP interface port: 8123 for plain HTTP, 8443 for HTTPS (ClickHouse Cloud) when not set explicitly.
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT") or (8443 if CLICKHOUSE_SECURE else 8123))
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USERNAME", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "imdb")

# schema.sql lives next to this script.
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
# Public bucket used by the ClickHouse docs' IMDB example dataset.
S3_BASE = "https://datasets-documentation.s3.eu-west-3.amazonaws.com/imdb"

# table -> (S3 file, columns to load)
# Each file is a gzipped TSV whose first row holds the column names
# (read with the 'TSVWithNames' input format). Only the listed columns are
# inserted; roles.created_at is filled by its DEFAULT now().
SOURCES = {
    "actors": ("imdb_ijs_actors.tsv.gz", ["id", "first_name", "last_name", "gender"]),
    "directors": ("imdb_ijs_directors.tsv.gz", ["id", "first_name", "last_name"]),
    "genres": ("imdb_ijs_movies_genres.tsv.gz", ["movie_id", "genre"]),
    "movie_directors": ("imdb_ijs_movies_directors.tsv.gz", ["director_id", "movie_id"]),
    "movies": ("imdb_ijs_movies.tsv.gz", ["id", "name", "year", "rank"]),
    "roles": ("imdb_ijs_roles.tsv.gz", ["actor_id", "movie_id", "role"]),
}


def schema_statements(database: str):
    """Read schema.sql and retarget it at the configured database name.

    Returns the list of SQL statements (without trailing semicolons).
    Lines starting with ``--`` are removed first, then the ``imdb`` database
    name is replaced by ``database`` (backquoted) and the text is split on ``;``.
    This simple splitter is why schema.sql must not contain semicolons inside
    SQL text.
    """
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    # Drop full-line comments (they may contain words like "imdb." or semicolons).
    sql = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    # Rename the database in CREATE DATABASE ...
    sql = sql.replace("CREATE DATABASE IF NOT EXISTS imdb;", f"CREATE DATABASE IF NOT EXISTS `{database}`;")
    # ... and in every fully qualified table name (imdb.movies -> `<database>`.movies).
    sql = re.sub(r"\bimdb\.(\w+)", lambda m: f"`{database}`.{m.group(1)}", sql)
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


def main():
    """Create the schema, then (re)load every table from S3 and report row counts."""
    print(f"Connecting to ClickHouse at {CLICKHOUSE_HOST}:{CLICKHOUSE_PORT} (secure={CLICKHOUSE_SECURE})")
    # Connect without a database first: the target database may not exist yet.
    client = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        secure=CLICKHOUSE_SECURE,
        # The roles load (~3.4M rows) can take minutes; allow up to 15 minutes per request.
        send_receive_timeout=900,
    )
    try:
        print("Creating schema")
        # CREATE DATABASE / CREATE TABLE ... IF NOT EXISTS, so re-running is safe.
        for stmt in schema_statements(CLICKHOUSE_DATABASE):
            client.command(stmt)

        for table, (file_name, columns) in SOURCES.items():
            cols = ", ".join(columns)
            started = time.perf_counter()
            # Empty the table first so a re-run does not duplicate rows.
            client.command(f"TRUNCATE TABLE IF EXISTS `{CLICKHOUSE_DATABASE}`.{table}")
            # Server-side load: ClickHouse reads the file from S3 with the s3() table function.
            client.command(
                f"INSERT INTO `{CLICKHOUSE_DATABASE}`.{table} ({cols}) "
                f"SELECT {cols} FROM s3('{S3_BASE}/{file_name}', 'TSVWithNames')"
            )
            rows = client.command(f"SELECT count() FROM `{CLICKHOUSE_DATABASE}`.{table}")
            print(f"Loaded {rows:>9,} rows into {CLICKHOUSE_DATABASE}.{table} "
                  f"({time.perf_counter() - started:.1f}s)")

        print("IMDB load complete")
    finally:
        client.close()


if __name__ == "__main__":
    main()
