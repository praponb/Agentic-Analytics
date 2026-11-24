"""Copy every table in the DuckDB `marts` schema into Postgres `warehouse.marts`.

This is the data path for the optional enterprise stack: dbt builds the star schema in
DuckDB, this script publishes it to Postgres, and Cube / Metabase read it from there.

Usage (after `dbt build` and `docker compose up -d`):
    uv run python scripts/publish_marts_to_postgres.py            # publish
    uv run python scripts/publish_marts_to_postgres.py --dry-run  # list tables only

Connection: PG_DSN, or POSTGRES_USER / POSTGRES_PASSWORD (+ optional PGHOST, PGPORT,
PGDATABASE). The values in docker/.env are read automatically if present.
Needs network access once to install DuckDB's postgres extension.
Close other writers of warehouse.duckdb first (it is attached read-only).
"""

import argparse
import os
import re
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _load_docker_env() -> None:
    env_file = ROOT / "docker" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _pg_dsn() -> str:
    if os.environ.get("PG_DSN"):
        return os.environ["PG_DSN"]
    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD")
    if not user or not password:
        raise SystemExit("Set PG_DSN or POSTGRES_USER/POSTGRES_PASSWORD (see docker/.env.example).")
    host = os.environ.get("PGHOST", "127.0.0.1")
    port = os.environ.get("PGPORT", "5432")
    dbname = os.environ.get("PGDATABASE", "warehouse")
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


def _redact(text: str) -> str:
    """Hide passwords in error text (DuckDB echoes the whole connection string on failure)."""
    text = re.sub(r"(password\s*=\s*)('[^']*'|[^\s'\"]+)", r"\1***", text, flags=re.IGNORECASE)
    text = re.sub(r"(postgres(?:ql)?://[^:/@\s]+:)[^@\s]+@", r"\1***@", text)
    password = os.environ.get("POSTGRES_PASSWORD")
    return text.replace(password, "***") if password else text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="List marts tables without connecting to Postgres")
    args = parser.parse_args()

    duckdb_path = Path(os.environ.get("DUCKDB_PATH") or ROOT / "warehouse.duckdb").resolve()
    if not duckdb_path.exists():
        raise SystemExit(f"{duckdb_path} not found. Run dbt (or run_dbt_models) first.")

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH {_sql_str(str(duckdb_path))} AS wh (READ_ONLY)")
        tables = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM duckdb_tables() "
                "WHERE database_name = 'wh' AND schema_name = 'marts' ORDER BY table_name"
            ).fetchall()
        ]
        if not tables:
            raise SystemExit("No tables found in schema 'marts'. Build the marts first.")
        print(f"marts tables in {duckdb_path.name}: {', '.join(tables)}")
        if args.dry_run:
            return

        _load_docker_env()
        con.execute("INSTALL postgres")
        con.execute("LOAD postgres")
        try:
            con.execute(f"ATTACH {_sql_str(_pg_dsn())} AS pg (TYPE postgres)")
        except duckdb.Error as exc:
            # `from None`: the chained traceback would print the DSN, password included
            raise SystemExit(
                f"Could not connect to Postgres: {_redact(str(exc))}\n"
                "Is the stack running? docker compose -f docker/docker-compose.yml up -d"
            ) from None
        try:
            con.execute("CREATE SCHEMA IF NOT EXISTS pg.marts")
            for table in tables:
                con.execute(f"DROP TABLE IF EXISTS pg.marts.{_ident(table)}")
                con.execute(f"CREATE TABLE pg.marts.{_ident(table)} AS SELECT * FROM wh.marts.{_ident(table)}")
                count = con.execute(f"SELECT count(*) FROM pg.marts.{_ident(table)}").fetchone()[0]
                print(f"  published marts.{table}: {count} rows")
        except duckdb.Error as exc:
            raise SystemExit(f"Publishing failed: {_redact(str(exc))}") from None
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
