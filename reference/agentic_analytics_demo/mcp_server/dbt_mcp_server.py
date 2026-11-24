"""Unified dbt + DuckDB MCP server (STDIO transport).

Strict rule: never write to sys.stdout in this module (no print()). FastMCP speaks
JSON-RPC over STDIO, so every diagnostic goes to sys.stderr via `logging`.

Security model
--------------
* `query_duckdb` is a trusted-analyst tool: it can read every table/view in
  warehouse.duckdb and every file under DATA_LAKE_DIR. It is NOT row-level secured;
  RLS lives in the Cube semantic layer (docker/cube_schema/cube.js).
* The file-system boundary is enforced by the DuckDB engine on every connection
  (`allowed_directories` + `enable_external_access = false` + `lock_configuration`).
  If that sandbox cannot be applied, the connection is refused (fail closed).
* SQL is classified by DuckDB's own parser (`extract_statements`), not by regex:
  exactly one SELECT-family or EXPLAIN statement is accepted.
"""

import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any, Callable

import anyio
import duckdb
import yaml
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dbt-duckdb-lakehouse")

mcp = FastMCP("dbt-duckdb-lakehouse")

# ------------------------------------------------------------------------------
# Configuration (environment variables override the defaults)
# ------------------------------------------------------------------------------
# Default to the directory that contains this file's parent, NOT the CWD: MCP clients
# may launch the server from "/" or $HOME.
_DEFAULT_PROJECT_DIR = Path(__file__).resolve().parent.parent
DBT_PROJECT_DIR = Path(os.environ.get("DBT_PROJECT_DIR") or _DEFAULT_PROJECT_DIR).resolve()
DBT_PROFILES_DIR = Path(os.environ.get("DBT_PROFILES_DIR") or DBT_PROJECT_DIR).resolve()
_VENV_DBT = Path(sys.executable).parent / ("dbt.exe" if os.name == "nt" else "dbt")
DBT_BIN = (
    os.environ.get("DBT_BIN")
    or (str(_VENV_DBT) if _VENV_DBT.exists() else None)
    or shutil.which("dbt")
    or "dbt"
)
DUCKDB_PATH = Path(
    os.environ.get("DUCKDB_PATH") or (DBT_PROJECT_DIR / "warehouse.duckdb")
).resolve()
DATA_LAKE_DIR = Path(
    os.environ.get("DATA_LAKE_DIR") or (DBT_PROJECT_DIR / "data_lake")
).resolve()

DBT_TIMEOUT_SEC = int(os.environ.get("DBT_TIMEOUT_SEC", "300"))
QUERY_TIMEOUT_SEC = float(os.environ.get("MCP_QUERY_TIMEOUT_SEC", "60"))
DUCKDB_MEMORY_LIMIT = os.environ.get("MCP_DUCKDB_MEMORY_LIMIT", "2GB")
DUCKDB_THREADS = int(os.environ.get("MCP_DUCKDB_THREADS", "4"))
MAX_ROWS_CAP = 500

# Serializes dbt CLI runs and DuckDB connections *inside this process*. DuckDB file locks
# across processes (DuckDB CLI, DBeaver, a second MCP server) are reported as
# `lock_conflict` instead of being prevented.
_DB_LOCK = threading.Lock()
LOCK_ERROR_MARKER = "Could not set lock on file"
LOCK_HINT = (
    "warehouse.duckdb is locked by another process (DuckDB CLI, DBeaver, Metabase, "
    "a second MCP server, or a dbt command started outside this server). Close it and retry."
)

# DuckDB parses SHOW / DESCRIBE / SUMMARIZE / VALUES / FROM-first queries as SELECT.
# DuckDB 1.x also rewrites read-only info pragmas (PRAGMA version, table_info, show_tables)
# to SELECT; state-changing pragmas stay PRAGMA and are rejected.
ALLOWED_STATEMENT_TYPES = {"SELECT", "EXPLAIN"}

NON_UNIQUE_TYPES = {
    "DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC", "BOOLEAN", "BLOB",
    "DATE", "TIME", "TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE",
    "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS", "INTERVAL",
}
CATEGORICAL_STRING_TYPES = {"VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR"}
# `unique` is only proposed for key-like column names; uniqueness of e.g. a date column in a
# small sample is coincidence, not a rule.
KEY_COLUMN_PATTERN = re.compile(r"^(id|.+_(id|key|code))$", re.IGNORECASE)
UNSAFE_LAKE_PATH_CHARS = set("'{}")


# ------------------------------------------------------------------------------
# Generic helpers
# ------------------------------------------------------------------------------
def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _sql_str(value: str) -> str:
    """Render a Python string as a single-quoted SQL literal."""
    return "'" + value.replace("'", "''") + "'"


def _quote_sql_ident(identifier: str) -> str:
    """Safely double-quote a SQL identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _error_payload(exc: BaseException | str, **extra: Any) -> dict[str, Any]:
    message = str(exc)
    payload: dict[str, Any] = {"success": False, "error": message, **extra}
    if LOCK_ERROR_MARKER in message:
        payload["lock_conflict"] = True
        payload["hint"] = LOCK_HINT
    return payload


def _ensure_project_cwd() -> None:
    """Keep CWD at DBT_PROJECT_DIR: dbt-duckdb views use paths relative to the project."""
    if DBT_PROJECT_DIR.is_dir() and Path.cwd().resolve() != DBT_PROJECT_DIR:
        os.chdir(DBT_PROJECT_DIR)


def _parse_dbt_selectors(select: str) -> tuple[bool, list[str] | str]:
    """Split a dbt selector string into argv tokens and block CLI flag injection."""
    tokens = select.strip().split()
    for tok in tokens:
        if tok.startswith("-"):
            return (
                False,
                f"Invalid selector token '{tok}': CLI flags starting with '-' are not allowed.",
            )
    return True, tokens


def _sanitize_identifier(name: str, prefix: str = "col") -> str:
    """Convert raw file or column names to safe SQL/YAML snake_case identifiers."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return f"{prefix}_unnamed"
    if cleaned[0].isdigit():
        cleaned = f"{prefix}_{cleaned}"
    return cleaned


def _test_kind(test: Any) -> str | None:
    """Return the generic test name of a dbt test entry ('unique', 'accepted_values', ...)."""
    if isinstance(test, str):
        return test
    if isinstance(test, dict) and test:
        return next(iter(test))
    return None


# ------------------------------------------------------------------------------
# DuckDB: sandboxed connections and SQL validation
# ------------------------------------------------------------------------------
def _allowed_directories() -> list[str]:
    """Absolute lake dir plus its project-relative form (dbt views use relative paths)."""
    dirs = [DATA_LAKE_DIR.as_posix().rstrip("/") + "/"]
    if DATA_LAKE_DIR.is_relative_to(DBT_PROJECT_DIR):
        rel = DATA_LAKE_DIR.relative_to(DBT_PROJECT_DIR).as_posix()
        if rel not in ("", "."):
            dirs.append(rel.rstrip("/") + "/")
    return dirs


def _connect(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open DuckDB (read-only file or in-memory) with the file-system sandbox enforced.

    Fails closed: raises if the sandbox cannot be applied and verified.
    """
    _ensure_project_cwd()
    if DATA_LAKE_DIR == DBT_PROJECT_DIR or DBT_PROJECT_DIR.is_relative_to(DATA_LAKE_DIR):
        raise RuntimeError(
            "DATA_LAKE_DIR must be a dedicated sub-directory, not the project directory "
            "or one of its parents (the sandbox would expose project files)."
        )

    config = {
        "threads": DUCKDB_THREADS,
        "memory_limit": DUCKDB_MEMORY_LIMIT,
    }
    if db_path is not None:
        con = duckdb.connect(str(db_path), read_only=True, config=config)
    else:
        con = duckdb.connect(":memory:", config=config)

    try:
        con.execute("SET enable_progress_bar = false")  # progress output must never reach stdout
        dirs = ", ".join(_sql_str(d) for d in _allowed_directories())
        con.execute(f"SET allowed_directories = [{dirs}]")
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        external = con.execute("SELECT current_setting('enable_external_access')").fetchone()
        if not external or str(external[0]).lower() != "false":
            raise RuntimeError("DuckDB sandbox verification failed (external access still enabled).")
    except Exception as exc:
        con.close()
        raise RuntimeError(
            f"Refusing to run: DuckDB file-system sandbox could not be applied "
            f"(duckdb {duckdb.__version__}; >= 1.3.0 required): {exc}"
        ) from exc
    return con


def _validate_readonly_sql(sql: str) -> tuple[bool, str]:
    """Accept exactly one SELECT-family or EXPLAIN statement, using DuckDB's own parser."""
    cleaned = sql.strip()
    if not cleaned:
        return False, "SQL query is empty."
    try:
        # Sandboxed: on DuckDB 1.x some statements (e.g. PRAGMA import_database) read files
        # while being parsed.
        parser = _connect(None)
        try:
            statements = parser.extract_statements(cleaned)
        finally:
            parser.close()
    except duckdb.Error as exc:
        return False, f"SQL parse error: {exc}"
    except RuntimeError as exc:
        return False, str(exc)

    if len(statements) != 1:
        return False, f"Exactly one SQL statement is allowed (found {len(statements)})."

    stmt_type = statements[0].type.name
    if stmt_type not in ALLOWED_STATEMENT_TYPES:
        return (
            False,
            f"Statement type '{stmt_type}' is not allowed in read-only mode. Allowed: SELECT "
            "(including FROM-first, SHOW, DESCRIBE, SUMMARIZE, VALUES) and EXPLAIN.",
        )
    return True, cleaned


def _resolve_lake_file(raw_path: str) -> Path | None:
    """Resolve an existing file strictly inside DATA_LAKE_DIR (symlinks resolved)."""
    cleaned = raw_path.strip()
    if not cleaned or "://" in cleaned or ".." in Path(cleaned).parts:
        return None

    candidate = Path(cleaned)
    search_candidates = (
        [candidate.resolve()]
        if candidate.is_absolute()
        else [(DATA_LAKE_DIR / candidate).resolve(), (DBT_PROJECT_DIR / candidate).resolve()]
    )
    for resolved in search_candidates:
        if resolved.is_relative_to(DATA_LAKE_DIR) and resolved.is_file():
            return resolved
    return None


# ------------------------------------------------------------------------------
# dbt CLI, manifest, run_results
# ------------------------------------------------------------------------------
def _parse_run_results(results_path: Path, include_code: bool = False) -> list[dict[str, Any]]:
    if not results_path.exists():
        return []
    data = json.loads(results_path.read_text(encoding="utf-8"))
    summary = []
    for res in data.get("results", []):
        item = {
            "unique_id": res.get("unique_id"),
            "status": res.get("status"),
            "execution_time_sec": round(res.get("execution_time") or 0, 2),
            "rows_affected": (res.get("adapter_response") or {}).get("rows_affected"),
            "failures": res.get("failures"),
            "message": res.get("message"),
        }
        if include_code:
            item["compiled_code"] = res.get("compiled_code")
        summary.append(item)
    return summary


def _execute_dbt_cli(
    args: list[str],
    timeout: int | None = None,
    collect_results: bool = False,
    include_code: bool = False,
) -> dict[str, Any]:
    """Run dbt without a shell, holding _DB_LOCK.

    run_results.json is deleted first and read back inside the lock, so a failed
    invocation can never report a previous run's results.
    """
    cmd = [
        DBT_BIN,
        *args,
        "--project-dir",
        str(DBT_PROJECT_DIR),
        "--profiles-dir",
        str(DBT_PROFILES_DIR),
    ]
    sub_env = {**os.environ, "DUCKDB_PATH": str(DUCKDB_PATH)}
    results_path = DBT_PROJECT_DIR / "target" / "run_results.json"
    effective_timeout = timeout or DBT_TIMEOUT_SEC

    with _DB_LOCK:
        results_path.unlink(missing_ok=True)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(DBT_PROJECT_DIR),
                env=sub_env,
                stdin=subprocess.DEVNULL,  # never inherit the MCP JSON-RPC stdin stream
                capture_output=True,       # never leak output onto the MCP stdout stream
                text=True,
                timeout=effective_timeout,
                check=False,
            )
        except FileNotFoundError:
            return {
                "returncode": -1,
                "success": False,
                "error": f"dbt executable not found at '{DBT_BIN}'. Set DBT_BIN in the MCP env config.",
                "node_results": [],
            }
        except subprocess.TimeoutExpired:
            return {
                "returncode": -1,
                "success": False,
                "error": f"dbt command timed out after {effective_timeout} seconds (DBT_TIMEOUT_SEC).",
                "node_results": [],
            }

        out: dict[str, Any] = {
            "returncode": proc.returncode,
            "success": proc.returncode == 0,
            "command": " ".join(cmd),
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-2000:],
        }
        if LOCK_ERROR_MARKER in proc.stdout or LOCK_ERROR_MARKER in proc.stderr:
            out["lock_conflict"] = True
            out["hint"] = LOCK_HINT
        if collect_results:
            out["node_results"] = _parse_run_results(results_path, include_code=include_code)
        return out


def _is_manifest_stale(manifest_path: Path) -> bool:
    """True if manifest.json is missing or older than any project SQL/YAML file."""
    if not manifest_path.exists():
        return True

    manifest_mtime = manifest_path.stat().st_mtime
    for wp in (DBT_PROJECT_DIR / "dbt_project.yml", DBT_PROFILES_DIR / "profiles.yml"):
        if wp.exists() and wp.stat().st_mtime > manifest_mtime:
            return True

    for subdir in ("models", "macros", "data_tests", "seeds", "snapshots", "analyses"):
        sub_path = DBT_PROJECT_DIR / subdir
        if not sub_path.is_dir():
            continue
        for file_p in sub_path.rglob("*"):
            if (
                file_p.is_file()
                and file_p.suffix.lower() in {".sql", ".yml", ".yaml", ".csv", ".py"}
                and file_p.stat().st_mtime > manifest_mtime
            ):
                return True
    return False


def _load_fresh_manifest(
    force_refresh: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Load target/manifest.json, running 'dbt parse' first if it is missing or stale."""
    manifest_path = DBT_PROJECT_DIR / "target" / "manifest.json"
    if force_refresh or _is_manifest_stale(manifest_path):
        parse_res = _execute_dbt_cli(["parse"])
        if not parse_res["success"]:
            return None, parse_res
    if not manifest_path.exists():
        return None, {"error": f"manifest.json not found at {manifest_path}"}
    return json.loads(manifest_path.read_text(encoding="utf-8")), None


# ------------------------------------------------------------------------------
# YAML property-file helpers
# ------------------------------------------------------------------------------
def _load_yaml_doc(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _write_yaml_doc(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _find_properties_file(model_name: str, fallback: Path) -> Path:
    """Return the YAML file under models/ that already documents `model_name`.

    Writing a second definition of the same model into another file is a dbt parse error.
    """
    models_dir = DBT_PROJECT_DIR / "models"
    if models_dir.is_dir():
        for yml in sorted([*models_dir.rglob("*.yml"), *models_dir.rglob("*.yaml")]):
            try:
                doc = _load_yaml_doc(yml)
            except yaml.YAMLError:
                continue
            for mdl in doc.get("models") or []:
                if isinstance(mdl, dict) and mdl.get("name") == model_name:
                    return yml
    return fallback


def _upsert_sources_yml(
    sources_path: Path,
    source_name: str,
    table_name: str,
    external_location: str,
    description: str,
    columns: list[dict[str, str]],
) -> None:
    """Merge a table entry into models/staging/sources.yml without touching other entries."""
    doc = _load_yaml_doc(sources_path)
    doc["version"] = 2
    if not isinstance(doc.get("sources"), list):
        doc["sources"] = []

    target_source = next(
        (s for s in doc["sources"] if isinstance(s, dict) and s.get("name") == source_name),
        None,
    )
    if target_source is None:
        target_source = {
            "name": source_name,
            "description": "Raw Parquet and CSV files from data_lake/",
            "tables": [],
        }
        doc["sources"].append(target_source)
    if not isinstance(target_source.get("tables"), list):
        target_source["tables"] = []

    new_table_entry: dict[str, Any] = {
        "name": table_name,
        "description": description,
        "meta": {"external_location": external_location},
        "columns": [
            {"name": col["raw_name"], "description": f"Source type: {col['data_type']}"}
            for col in columns
        ],
    }
    for idx, tbl in enumerate(target_source["tables"]):
        if isinstance(tbl, dict) and tbl.get("name") == table_name:
            target_source["tables"][idx] = new_table_entry
            break
    else:
        target_source["tables"].append(new_table_entry)

    _write_yaml_doc(sources_path, doc)


def _upsert_model_schema_yml(
    schema_yml_path: Path,
    model_name: str,
    default_model_desc: str,
    profiled_columns: list[dict[str, Any]],
    overwrite_descriptions: bool = False,
) -> dict[str, Any]:
    """Merge columns and inferred tests into a model's YAML without losing manual edits.

    * Existing column keys (meta, constraints, description, ...) are kept.
    * Existing tests are kept; an inferred test is added only if no test of the same kind
      (unique / not_null / accepted_values / ...) is already present.
    * Columns that no longer exist in the relation are kept and reported as stale.
    """
    doc = _load_yaml_doc(schema_yml_path)
    doc["version"] = 2
    if not isinstance(doc.get("models"), list):
        doc["models"] = []

    target_model = next(
        (m for m in doc["models"] if isinstance(m, dict) and m.get("name") == model_name),
        None,
    )
    if target_model is None:
        target_model = {"name": model_name, "description": default_model_desc, "columns": []}
        doc["models"].append(target_model)
    elif not target_model.get("description") or overwrite_descriptions:
        target_model["description"] = default_model_desc

    existing_cols: dict[str, dict[str, Any]] = {
        c["name"]: c
        for c in (target_model.get("columns") or [])
        if isinstance(c, dict) and c.get("name")
    }

    merged: list[dict[str, Any]] = []
    added_tests: dict[str, list[Any]] = {}
    profiled_names: set[str] = set()
    for col_info in profiled_columns:
        name = col_info["name"]
        profiled_names.add(name)
        entry = existing_cols.get(name) or {"name": name}

        generated_desc = col_info.get("description") or f"Column ({col_info.get('data_type', 'UNKNOWN')})"
        if overwrite_descriptions or not entry.get("description"):
            entry["description"] = generated_desc

        tests_key = "tests" if ("tests" in entry and "data_tests" not in entry) else "data_tests"
        tests = list(entry.get(tests_key) or [])
        present = {_test_kind(t) for t in tests}
        new_tests = [t for t in col_info.get("inferred_tests", []) if _test_kind(t) not in present]
        if new_tests:
            tests.extend(new_tests)
            added_tests[name] = new_tests
        if tests:
            entry[tests_key] = tests
        merged.append(entry)

    stale = [col for name, col in existing_cols.items() if name not in profiled_names]
    target_model["columns"] = merged + stale
    _write_yaml_doc(schema_yml_path, doc)
    return {"added_tests": added_tests, "stale_columns": [c["name"] for c in stale]}


# ==============================================================================
# 1. dbt modeling, scaffolding, testing & pipeline tools
# ==============================================================================
def create_staging_model_from_lake(
    lake_file_path: str,
    table_name: str = "",
    source_name: str = "data_lake",
    materialized: str = "view",
    overwrite: bool = False,
) -> str:
    """
    Inspect a Parquet or CSV file in data_lake/, register it in models/staging/sources.yml
    with external_location, generate models/staging/stg_<source>__<table>.sql with snake_case
    column aliases, and run 'dbt parse' to validate. CSV files must be UTF-8 encoded.
    """
    resolved_file = _resolve_lake_file(lake_file_path)
    if resolved_file is None:
        return _json({"success": False, "error": f"File '{lake_file_path}' not found inside {DATA_LAKE_DIR}."})

    ext = resolved_file.suffix.lower()
    if ext not in {".parquet", ".csv"}:
        return _json({"success": False, "error": f"Only .parquet and .csv files are supported (received '{ext}')."})

    if resolved_file.is_relative_to(DBT_PROJECT_DIR):
        rel_path_posix = resolved_file.relative_to(DBT_PROJECT_DIR).as_posix()
    else:
        rel_path_posix = resolved_file.as_posix()

    if UNSAFE_LAKE_PATH_CHARS & set(rel_path_posix):
        return _json({
            "success": False,
            "error": "Lake file paths must not contain quotes or curly braces "
                     f"({sorted(UNSAFE_LAKE_PATH_CHARS)}); rename the file.",
        })

    clean_source = _sanitize_identifier(source_name, prefix="src")
    clean_table = _sanitize_identifier(
        table_name if table_name.strip() else resolved_file.stem, prefix="tbl"
    )
    clean_mat = materialized.strip().lower()
    if clean_mat not in {"view", "table", "ephemeral"}:
        clean_mat = "view"

    reader_fn = "read_parquet" if ext == ".parquet" else "read_csv_auto"
    external_location = f"{reader_fn}('{rel_path_posix}')"

    try:
        con = _connect(None)
        try:
            desc_rows = con.execute(
                f"DESCRIBE SELECT * FROM {reader_fn}(?)", [str(resolved_file)]
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:
        return _json({"success": False, "error": f"Failed to inspect schema: {exc}"})

    if not desc_rows:
        return _json({"success": False, "error": "No columns detected in the specified file."})

    columns_meta: list[dict[str, str]] = []
    select_expressions: list[str] = []
    seen_aliases: set[str] = set()
    for raw_col_name, col_type, *_ in desc_rows:
        safe_alias = _sanitize_identifier(str(raw_col_name), prefix="col")
        if safe_alias in seen_aliases:
            suffix = 2
            while f"{safe_alias}_{suffix}" in seen_aliases:
                suffix += 1
            safe_alias = f"{safe_alias}_{suffix}"
        seen_aliases.add(safe_alias)

        columns_meta.append(
            {"raw_name": str(raw_col_name), "alias": safe_alias, "data_type": str(col_type)}
        )
        if str(raw_col_name) == safe_alias:
            select_expressions.append(f"        {safe_alias}")
        else:
            select_expressions.append(f"        {_quote_sql_ident(str(raw_col_name))} as {safe_alias}")

    staging_dir = DBT_PROJECT_DIR / "models" / "staging"
    sources_path = staging_dir / "sources.yml"
    model_path = staging_dir / f"stg_{clean_source}__{clean_table}.sql"

    if model_path.exists() and not overwrite:
        return _json({
            "success": False,
            "error": f"File '{model_path.relative_to(DBT_PROJECT_DIR)}' already exists (set overwrite=True to replace).",
        })

    _upsert_sources_yml(
        sources_path=sources_path,
        source_name=clean_source,
        table_name=clean_table,
        external_location=external_location,
        description=f"Auto-registered from {rel_path_posix}",
        columns=columns_meta,
    )

    select_block = ",\n".join(select_expressions)
    sql_content = (
        f"{{{{\n"
        f"    config(\n"
        f"        materialized='{clean_mat}'\n"
        f"    )\n"
        f"}}}}\n\n"
        f"with source as (\n\n"
        f"    select * from {{{{ source('{clean_source}', '{clean_table}') }}}}\n\n"
        f"),\n\n"
        f"renamed as (\n\n"
        f"    select\n"
        f"{select_block}\n"
        f"    from source\n\n"
        f")\n\n"
        f"select * from renamed\n"
    )
    staging_dir.mkdir(parents=True, exist_ok=True)
    model_path.write_text(sql_content, encoding="utf-8")

    parse_res = _execute_dbt_cli(["parse"])
    return _json({
        "success": True,
        "model_file": str(model_path.relative_to(DBT_PROJECT_DIR)),
        "sources_file": str(sources_path.relative_to(DBT_PROJECT_DIR)),
        "source_reference": f"{{{{ source('{clean_source}', '{clean_table}') }}}}",
        "external_location": external_location,
        "column_count": len(columns_meta),
        "generated_sql": sql_content,
        "dbt_parse_valid": parse_res["success"],
        "dbt_parse_details": None if parse_res["success"] else parse_res,
    })


def create_dim_date_model(
    start_date: str = "2020-01-01",
    end_date: str = "2030-12-31",
    fiscal_year_start_month: int = 10,
    fiscal_year_naming: str = "end",
    materialized: str = "table",
    overwrite: bool = False,
) -> str:
    """
    Generate models/marts/dim_date.sql for Time Intelligence with a configurable Fiscal Year
    and register column docs + data_tests in the marts property YAML.
    - fiscal_year_start_month: 1-12 (default 10 = October)
    - fiscal_year_naming: 'end' (default; FY named after the calendar year it ends in, e.g.
      Oct 2025 -> FY2026, Thai government / US federal style) or 'start' (Oct 2025 -> FY2025).
    """
    try:
        parsed_start = date.fromisoformat(start_date.strip())
        parsed_end = date.fromisoformat(end_date.strip())
    except ValueError as exc:
        return _json({"success": False, "error": f"Invalid ISO date (expected YYYY-MM-DD): {exc}"})

    if parsed_start > parsed_end:
        return _json({
            "success": False,
            "error": f"start_date ({parsed_start}) cannot be later than end_date ({parsed_end}).",
        })

    fy_month = int(fiscal_year_start_month)
    if not 1 <= fy_month <= 12:
        return _json({"success": False, "error": "fiscal_year_start_month must be between 1 and 12."})

    naming = fiscal_year_naming.strip().lower()
    if naming not in {"end", "start"}:
        return _json({"success": False, "error": "fiscal_year_naming must be 'end' or 'start'."})

    clean_mat = materialized.strip().lower()
    if clean_mat not in {"table", "view"}:
        clean_mat = "table"

    marts_dir = DBT_PROJECT_DIR / "models" / "marts"
    model_path = marts_dir / "dim_date.sql"
    schema_yml_path = _find_properties_file("dim_date", marts_dir / "schema.yml")

    if model_path.exists() and not overwrite:
        return _json({
            "success": False,
            "error": f"File '{model_path.relative_to(DBT_PROJECT_DIR)}' already exists (set overwrite=True to replace).",
        })

    start_iso = parsed_start.isoformat()
    end_iso = parsed_end.isoformat()

    # Year offsets for months on/after vs. before the fiscal start month.
    on_or_after_offset = 1 if naming == "end" else 0
    before_offset = 0 if naming == "end" else -1
    if fy_month == 1:
        fiscal_year_expr = "extract(year from date_day)::integer"
    else:
        fiscal_year_expr = (
            f"case\n"
            f"            when extract(month from date_day) >= {fy_month}\n"
            f"                then extract(year from date_day)::integer + ({on_or_after_offset})\n"
            f"            else extract(year from date_day)::integer + ({before_offset})\n"
            f"        end"
        )

    sql_content = (
        f"{{{{\n"
        f"    config(\n"
        f"        materialized='{clean_mat}'\n"
        f"    )\n"
        f"}}}}\n\n"
        f"with date_spine as (\n\n"
        f"    select\n"
        f"        unnest(\n"
        f"            generate_series(\n"
        f"                cast('{start_iso}' as date),\n"
        f"                cast('{end_iso}' as date),\n"
        f"                interval '1 day'\n"
        f"            )\n"
        f"        )::date as date_day\n\n"
        f"),\n\n"
        f"base_dates as (\n\n"
        f"    select\n"
        f"        cast(strftime(date_day, '%Y%m%d') as integer) as date_key,\n"
        f"        date_day,\n"
        f"        extract(year from date_day)::integer as calendar_year,\n"
        f"        extract(quarter from date_day)::integer as calendar_quarter,\n"
        f"        extract(month from date_day)::integer as month_number,\n"
        f"        strftime(date_day, '%B') as month_name,\n"
        f"        strftime(date_day, '%b') as month_short_name,\n"
        f"        strftime(date_day, '%Y-%m') as year_month,\n"
        f"        extract(day from date_day)::integer as day_of_month,\n"
        f"        extract(isodow from date_day)::integer as day_of_week,\n"
        f"        strftime(date_day, '%A') as day_name,\n"
        f"        strftime(date_day, '%a') as day_short_name,\n"
        f"        extract(isodow from date_day) in (6, 7) as is_weekend,\n"
        f"        {fiscal_year_expr} as fiscal_year,\n"
        f"        (((extract(month from date_day)::integer - {fy_month} + 12) % 12) + 1)::integer as fiscal_month_number,\n"
        f"        (floor(((extract(month from date_day)::integer - {fy_month} + 12) % 12) / 3.0) + 1)::integer as fiscal_quarter\n"
        f"    from date_spine\n\n"
        f"),\n\n"
        f"final as (\n\n"
        f"    select\n"
        f"        date_key,\n"
        f"        date_day,\n"
        f"        calendar_year,\n"
        f"        calendar_quarter,\n"
        f"        'Q' || calendar_quarter::varchar as calendar_quarter_name,\n"
        f"        month_number,\n"
        f"        month_name,\n"
        f"        month_short_name,\n"
        f"        year_month,\n"
        f"        day_of_month,\n"
        f"        day_of_week,\n"
        f"        day_name,\n"
        f"        day_short_name,\n"
        f"        is_weekend,\n"
        f"        fiscal_year,\n"
        f"        'FY' || fiscal_year::varchar as fiscal_year_label,\n"
        f"        fiscal_quarter,\n"
        f"        'FY' || fiscal_year::varchar || '-Q' || fiscal_quarter::varchar as fiscal_quarter_label,\n"
        f"        fiscal_month_number,\n"
        f"        'FY' || fiscal_year::varchar || '-M' || lpad(fiscal_month_number::varchar, 2, '0') as fiscal_year_month\n"
        f"    from base_dates\n\n"
        f")\n\n"
        f"select * from final\n"
    )
    marts_dir.mkdir(parents=True, exist_ok=True)
    model_path.write_text(sql_content, encoding="utf-8")

    if fy_month == 1:
        fy_desc = "Fiscal year integer (identical to calendar year; fiscal year starts in January)"
    elif naming == "end":
        fy_desc = (
            f"Fiscal year integer, named after the year it ends in (starts in month {fy_month}; "
            f"months >= {fy_month} map to calendar_year + 1)"
        )
    else:
        fy_desc = (
            f"Fiscal year integer, named after the year it starts in (starts in month {fy_month}; "
            f"months < {fy_month} map to calendar_year - 1)"
        )
    quarter_test = {"accepted_values": {"values": [1, 2, 3, 4], "quote": False}}

    def col(name: str, dtype: str, desc: str, tests: list[Any] | None = None) -> dict[str, Any]:
        return {"name": name, "data_type": dtype, "description": desc,
                "inferred_tests": tests if tests is not None else ["not_null"]}

    dim_date_columns = [
        col("date_key", "INTEGER", "Surrogate key in YYYYMMDD integer format", ["unique", "not_null"]),
        col("date_day", "DATE", "Calendar date (grain: 1 row per day)", ["unique", "not_null"]),
        col("calendar_year", "INTEGER", "Calendar year (e.g., 2026)"),
        col("calendar_quarter", "INTEGER", "Calendar quarter (1 to 4)", ["not_null", quarter_test]),
        col("calendar_quarter_name", "VARCHAR", "Calendar quarter label (Q1 to Q4)"),
        col("month_number", "INTEGER", "Calendar month number (1 to 12)"),
        col("month_name", "VARCHAR", "Full English month name"),
        col("month_short_name", "VARCHAR", "Abbreviated month name (Jan, Feb, ...)"),
        col("year_month", "VARCHAR", "Year-Month string (YYYY-MM)"),
        col("day_of_month", "INTEGER", "Day of month (1 to 31)"),
        col("day_of_week", "INTEGER", "ISO day of week (1=Monday to 7=Sunday)"),
        col("day_name", "VARCHAR", "Full day name (Monday to Sunday)"),
        col("day_short_name", "VARCHAR", "Abbreviated day name (Mon to Sun)"),
        col("is_weekend", "BOOLEAN", "True if Saturday or Sunday"),
        col("fiscal_year", "INTEGER", fy_desc),
        col("fiscal_year_label", "VARCHAR", "Fiscal year label (e.g., FY2026)"),
        col("fiscal_quarter", "INTEGER", f"Fiscal quarter number (1 to 4, Q1 starts in month {fy_month})",
            ["not_null", quarter_test]),
        col("fiscal_quarter_label", "VARCHAR", "Fiscal year and quarter label (e.g., FY2026-Q1)"),
        col("fiscal_month_number", "INTEGER", f"Fiscal month sequence (1 to 12, where 1 = month {fy_month})"),
        col("fiscal_year_month", "VARCHAR", "Fiscal year and month label (e.g., FY2026-M01)"),
    ]

    merge_info = _upsert_model_schema_yml(
        schema_yml_path=schema_yml_path,
        model_name="dim_date",
        default_model_desc=(
            f"Date dimension ({start_iso} to {end_iso}); fiscal year starts in month {fy_month}, "
            f"named by its {naming} year."
        ),
        profiled_columns=dim_date_columns,
        overwrite_descriptions=True,  # the tool owns dim_date's generated docs
    )

    parse_res = _execute_dbt_cli(["parse"])
    return _json({
        "success": True,
        "model_file": str(model_path.relative_to(DBT_PROJECT_DIR)),
        "schema_yml_file": str(schema_yml_path.relative_to(DBT_PROJECT_DIR)),
        "date_range": {
            "start_date": start_iso,
            "end_date": end_iso,
            "total_days": (parsed_end - parsed_start).days + 1,
        },
        "fiscal_year_start_month": fy_month,
        "fiscal_year_naming": naming,
        "stale_columns": merge_info["stale_columns"],
        "dbt_parse_valid": parse_res["success"],
        "generated_sql": sql_content,
        "dbt_parse_details": None if parse_res["success"] else parse_res,
    })


def generate_dbt_schema_tests(
    model_name: str,
    max_categorical_values: int = 15,
    dry_run: bool = False,
) -> str:
    """
    Profile a built dbt model in DuckDB and propose data_tests:
    - unique: only key-like columns (id, *_id, *_key, *_code) with no nulls/duplicates
    - not_null: columns with no nulls
    - accepted_values: low-cardinality text columns, emitted with severity 'warn'
    Merges into the YAML file that already documents the model (manual tests/meta kept).
    dry_run=True returns the proposal without writing. The model must be built first.
    """
    manifest, parse_err = _load_fresh_manifest()
    if manifest is None:
        return _json({"success": False, "error": "Failed to parse dbt manifest.", "details": parse_err})

    target_node = next(
        (
            n for n in manifest.get("nodes", {}).values()
            if n.get("resource_type") == "model" and n.get("name") == model_name.strip()
        ),
        None,
    )
    if target_node is None:
        return _json({
            "success": False,
            "error": f"Model '{model_name}' not found in dbt manifest. Check names via get_dbt_metadata.",
        })

    if not DUCKDB_PATH.exists():
        return _json({"success": False, "error": f"DuckDB file not found at '{DUCKDB_PATH}'. Run run_dbt_models first."})

    db_schema = target_node.get("schema", "main")
    db_relation = target_node.get("alias") or target_node.get("name")
    model_file_path = (DBT_PROJECT_DIR / target_node.get("original_file_path", "")).resolve()
    schema_yml_path = _find_properties_file(model_name.strip(), model_file_path.parent / "schema.yml")
    safe_cat_limit = max(2, min(int(max_categorical_values), 50))
    qualified_table = f"{_quote_sql_ident(db_schema)}.{_quote_sql_ident(db_relation)}"

    try:
        with _DB_LOCK:
            con = _connect(DUCKDB_PATH)
            try:
                col_rows = con.execute(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE LOWER(table_schema) = LOWER(?)
                      AND LOWER(table_name) = LOWER(?)
                    ORDER BY ordinal_position
                    """,
                    [db_schema, db_relation],
                ).fetchall()
                if not col_rows:
                    return _json({
                        "success": False,
                        "error": f"Relation '{db_schema}.{db_relation}' not found in DuckDB. "
                                 f"Run run_dbt_models(select='{model_name}') first.",
                    })

                agg_exprs = ["COUNT(*) AS __total_rows"]
                for idx, (col_name, _) in enumerate(col_rows):
                    q_col = _quote_sql_ident(col_name)
                    agg_exprs.append(f"COUNT({q_col}) AS __non_null_{idx}")
                    agg_exprs.append(f"COUNT(DISTINCT {q_col}) AS __distinct_{idx}")
                stats_row = con.execute(f"SELECT {', '.join(agg_exprs)} FROM {qualified_table}").fetchone()

                total_rows = int(stats_row[0]) if stats_row else 0
                profiled_columns: list[dict[str, Any]] = []
                for idx, (col_name, data_type) in enumerate(col_rows):
                    non_null_count = int(stats_row[1 + idx * 2])
                    distinct_count = int(stats_row[2 + idx * 2])
                    null_count = total_rows - non_null_count
                    base_type = data_type.split("(")[0].strip().upper()
                    inferred: list[Any] = []

                    if (
                        total_rows > 1
                        and null_count == 0
                        and distinct_count == total_rows
                        and base_type not in NON_UNIQUE_TYPES
                        and KEY_COLUMN_PATTERN.match(col_name)
                    ):
                        inferred.append("unique")

                    if total_rows > 0 and null_count == 0:
                        inferred.append("not_null")

                    if (
                        base_type in CATEGORICAL_STRING_TYPES
                        and 1 <= distinct_count <= safe_cat_limit
                        and total_rows > distinct_count
                        and not KEY_COLUMN_PATTERN.match(col_name)
                    ):
                        q_col = _quote_sql_ident(col_name)
                        values = [
                            str(r[0])
                            for r in con.execute(
                                f"SELECT DISTINCT {q_col} FROM {qualified_table} "
                                f"WHERE {q_col} IS NOT NULL ORDER BY 1 LIMIT {safe_cat_limit}"
                            ).fetchall()
                        ]
                        # dbt does not escape quotes inside accepted_values
                        if values and not any("'" in v for v in values):
                            inferred.append({
                                "accepted_values": {
                                    "values": values,
                                    "config": {"severity": "warn"},
                                }
                            })

                    profiled_columns.append({
                        "name": col_name,
                        "data_type": data_type,
                        "total_rows": total_rows,
                        "null_count": null_count,
                        "distinct_count": distinct_count,
                        "inferred_tests": inferred,
                    })
            finally:
                con.close()
    except Exception as exc:
        return _json(_error_payload(f"DuckDB profiling error: {exc}"))

    if dry_run:
        return _json({
            "success": True,
            "dry_run": True,
            "model_name": model_name,
            "target_yaml_file": str(schema_yml_path.relative_to(DBT_PROJECT_DIR)),
            "total_rows_profiled": total_rows,
            "column_profiling_stats": profiled_columns,
        })

    merge_info = _upsert_model_schema_yml(
        schema_yml_path=schema_yml_path,
        model_name=model_name.strip(),
        default_model_desc=target_node.get("description") or f"Auto-profiled dbt model: {model_name}",
        profiled_columns=profiled_columns,
    )
    parse_res = _execute_dbt_cli(["parse"])

    return _json({
        "success": parse_res["success"],
        "model_name": model_name,
        "relation_profiled": f"{db_schema}.{db_relation}",
        "total_rows_profiled": total_rows,
        "schema_yml_file": str(schema_yml_path.relative_to(DBT_PROJECT_DIR)),
        "added_tests": merge_info["added_tests"],
        "stale_columns": merge_info["stale_columns"],
        "dbt_parse_valid": parse_res["success"],
        "dbt_parse_details": None if parse_res["success"] else parse_res,
        "column_profiling_stats": profiled_columns,
    })


def get_dbt_metadata(model_name: str = "", force_refresh: bool = False) -> str:
    """
    Read dbt model metadata (schema, materialization, descriptions, columns, lineage)
    from target/manifest.json. Automatically runs 'dbt parse' if the manifest is missing or stale.
    """
    manifest, parse_err = _load_fresh_manifest(force_refresh=force_refresh)
    if manifest is None:
        return _json({"success": False, "error": "Failed to parse dbt manifest.", "details": parse_err})

    models_info = []
    for unique_id, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "model":
            continue
        name = node.get("name", "")
        if model_name and model_name.lower() not in name.lower():
            continue
        models_info.append({
            "name": name,
            "unique_id": unique_id,
            "schema": node.get("schema"),
            "materialized": node.get("config", {}).get("materialized"),
            "description": node.get("description", ""),
            "file_path": node.get("original_file_path"),
            "properties_file": node.get("patch_path"),
            "depends_on": node.get("depends_on", {}).get("nodes", []),
            "columns": {
                col_name: {
                    "description": col_meta.get("description", ""),
                    "data_type": col_meta.get("data_type", ""),
                }
                for col_name, col_meta in node.get("columns", {}).items()
            },
        })
    return _json({"success": True, "total_models": len(models_info), "models": models_info})


def compile_dbt_model(select: str) -> str:
    """
    Run 'dbt compile' for any valid dbt selector (e.g., 'dim_date', '+fact_sales', 'staging.*')
    and return the exact compiled SQL per node (from run_results.json / target/compiled/).
    """
    ok, parsed = _parse_dbt_selectors(select)
    if not ok or not parsed:
        return _json({"success": False, "error": parsed if not ok else "select cannot be empty."})

    cli_res = _execute_dbt_cli(
        ["compile", "--select", *parsed], collect_results=True, include_code=True
    )
    if not cli_res["success"]:
        return _json(cli_res)

    manifest_path = DBT_PROJECT_DIR / "target" / "manifest.json"
    manifest_nodes = (
        json.loads(manifest_path.read_text(encoding="utf-8")).get("nodes", {})
        if manifest_path.exists()
        else {}
    )
    compiled_sql: dict[str, str] = {}
    for res in cli_res["node_results"]:
        uid = res.get("unique_id") or ""
        if res.get("compiled_code"):
            compiled_sql[uid] = res["compiled_code"]
            continue
        c_path = manifest_nodes.get(uid, {}).get("compiled_path")
        if c_path and (DBT_PROJECT_DIR / c_path).exists():
            compiled_sql[uid] = (DBT_PROJECT_DIR / c_path).read_text(encoding="utf-8")

    return _json({"success": True, "compiled_count": len(compiled_sql), "compiled_sql": compiled_sql})


def run_dbt_models(select: str = "", full_refresh: bool = False) -> str:
    """
    Execute 'dbt run' to build or update models in DuckDB.
    - select: optional dbt selector(s), e.g., 'dim_date', '+fact_sales', 'staging.*'
    - full_refresh: pass --full-refresh for incremental models
    """
    args = ["run"]
    if select.strip():
        ok, parsed = _parse_dbt_selectors(select)
        if not ok:
            return _json({"success": False, "error": parsed})
        args.extend(["--select", *parsed])
    if full_refresh:
        args.append("--full-refresh")

    cli_res = _execute_dbt_cli(args, collect_results=True)
    output: dict[str, Any] = {
        "success": cli_res["success"],
        "command": cli_res.get("command"),
        "node_results": cli_res.get("node_results", []),
    }
    for key in ("lock_conflict", "hint"):
        if key in cli_res:
            output[key] = cli_res[key]
    if not cli_res["success"]:
        output["logs"] = cli_res.get("stdout") or cli_res.get("stderr") or cli_res.get("error")
    return _json(output)


def run_dbt_tests(select: str = "") -> str:
    """
    Execute 'dbt test'. 'fail'/'error' results count as failures; 'warn' results
    (e.g. auto-generated accepted_values) are reported separately and do not fail the run.
    """
    args = ["test"]
    if select.strip():
        ok, parsed = _parse_dbt_selectors(select)
        if not ok:
            return _json({"success": False, "error": parsed})
        args.extend(["--select", *parsed])

    cli_res = _execute_dbt_cli(args, collect_results=True)
    results = cli_res.get("node_results", [])
    failed = [r for r in results if r["status"] in {"fail", "error"}]
    warned = [r for r in results if r["status"] == "warn"]

    output: dict[str, Any] = {
        "success": cli_res["success"] and not failed,
        "total_tests": len(results),
        "failed_count": len(failed),
        "warn_count": len(warned),
        "failed_details": failed,
        "warn_details": warned,
        "all_results": results,
    }
    for key in ("lock_conflict", "hint"):
        if key in cli_res:
            output[key] = cli_res[key]
    if not cli_res["success"] and not failed:
        output["logs"] = cli_res.get("stdout") or cli_res.get("stderr") or cli_res.get("error")
    return _json(output)


# ==============================================================================
# 2. DuckDB & data lake discovery / read-only analytics tools
# ==============================================================================
def list_duckdb_tables(schema_name: str = "") -> str:
    """List all schemas, tables, views, and columns inside warehouse.duckdb."""
    if not DUCKDB_PATH.exists():
        return _json({
            "success": True,
            "warning": f"DuckDB file not yet created at '{DUCKDB_PATH}'. "
                       "Use list_data_lake_files to inspect data_lake/ or run_dbt_models first.",
            "tables": [],
        })

    sql = """
        SELECT t.table_schema, t.table_name, t.table_type,
               c.column_name, c.data_type, c.ordinal_position
        FROM information_schema.tables t
        LEFT JOIN information_schema.columns c
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE t.table_schema NOT IN ('information_schema', 'pg_catalog')
    """
    params: list[Any] = []
    if schema_name.strip():
        sql += " AND LOWER(t.table_schema) = LOWER(?)"
        params.append(schema_name.strip())
    sql += " ORDER BY t.table_schema, t.table_name, c.ordinal_position"

    try:
        with _DB_LOCK:
            con = _connect(DUCKDB_PATH)
            try:
                rows = con.execute(sql, params).fetchall()
            finally:
                con.close()
    except Exception as exc:
        return _json(_error_payload(f"Failed to inspect DuckDB catalog: {exc}"))

    tables_map: dict[str, dict[str, Any]] = {}
    for t_schema, t_name, t_type, col_name, data_type, _ in rows:
        entry = tables_map.setdefault(
            f"{t_schema}.{t_name}",
            {"schema": t_schema, "table": t_name, "type": t_type, "columns": []},
        )
        if col_name:
            entry["columns"].append({"name": col_name, "type": data_type})

    return _json({
        "success": True,
        "duckdb_path": str(DUCKDB_PATH),
        "total_tables": len(tables_map),
        "tables": list(tables_map.values()),
    })


def list_data_lake_files(filter_keyword: str = "", include_schema: bool = True) -> str:
    """
    Scan Parquet and CSV files under data_lake/, returning project-relative query paths
    (use these exact 'data_lake/...' paths in SQL), file sizes, and inferred column schemas.
    """
    if not DATA_LAKE_DIR.exists():
        return _json({
            "success": True,
            "data_lake_dir": str(DATA_LAKE_DIR),
            "total_files": 0,
            "message": "data_lake/ directory does not exist yet.",
        })

    matched_files = sorted(
        p for p in DATA_LAKE_DIR.rglob("*")
        if p.is_file()
        and p.suffix.lower() in {".parquet", ".csv"}
        and (not filter_keyword or filter_keyword.lower() in p.name.lower())
    )

    files_info = []
    con = _connect(None) if include_schema else None
    try:
        for file_path in matched_files:
            ext = file_path.suffix.lower()
            if file_path.is_relative_to(DBT_PROJECT_DIR):
                query_path = file_path.relative_to(DBT_PROJECT_DIR).as_posix()
            else:
                query_path = file_path.as_posix()
            entry: dict[str, Any] = {
                "file_name": file_path.name,
                "format": ext.lstrip("."),
                "query_path": query_path,
                "size_kb": round(file_path.stat().st_size / 1024, 2),
            }
            if con is not None:
                try:
                    reader_fn = "read_parquet" if ext == ".parquet" else "read_csv_auto"
                    desc_rows = con.execute(
                        f"DESCRIBE SELECT * FROM {reader_fn}(?)", [str(file_path)]
                    ).fetchall()
                    entry["columns"] = [{"name": r[0], "type": r[1]} for r in desc_rows]
                except Exception as schema_err:
                    entry["schema_error"] = str(schema_err)
            files_info.append(entry)
    finally:
        if con is not None:
            con.close()

    return _json({
        "success": True,
        "data_lake_dir": str(DATA_LAKE_DIR),
        "total_files": len(files_info),
        "files": files_info,
    })


def query_duckdb(sql: str, max_rows: int = 100) -> str:
    """
    Execute ONE read-only SQL statement across warehouse.duckdb tables/views and
    Parquet/CSV files in data_lake/ (reference files as 'data_lake/<file>').
    Allowed: SELECT (incl. WITH, FROM-first, SHOW, DESCRIBE, SUMMARIZE, VALUES) and EXPLAIN.
    - max_rows: rows returned (default 100, capped at 500). Queries are cancelled after
      MCP_QUERY_TIMEOUT_SEC seconds.
    """
    is_valid, result_or_err = _validate_readonly_sql(sql)
    if not is_valid:
        return _json({"success": False, "error": result_or_err})

    cleaned_sql = result_or_err
    safe_limit = max(1, min(int(max_rows), MAX_ROWS_CAP))
    timed_out = threading.Event()

    try:
        with _DB_LOCK:
            use_file = DUCKDB_PATH.exists()
            con = _connect(DUCKDB_PATH if use_file else None)
            connection_mode = (
                f"file:{DUCKDB_PATH} (read-only)" if use_file
                else "in-memory (warehouse.duckdb not yet created)"
            )

            def _cancel() -> None:
                timed_out.set()
                con.interrupt()

            timer = threading.Timer(QUERY_TIMEOUT_SEC, _cancel)
            timer.daemon = True
            timer.start()
            try:
                rel = con.execute(cleaned_sql)
                columns = [d[0] for d in rel.description] if rel.description else []
                fetched = rel.fetchmany(safe_limit + 1)
            finally:
                timer.cancel()
                con.close()
    except Exception as exc:
        if timed_out.is_set():
            return _json({
                "success": False,
                "timed_out": True,
                "error": f"Query cancelled after {QUERY_TIMEOUT_SEC:g}s (MCP_QUERY_TIMEOUT_SEC).",
                "sql_executed": cleaned_sql,
            })
        return _json(_error_payload(exc, sql_executed=cleaned_sql))

    records = [dict(zip(columns, row)) for row in fetched[:safe_limit]]
    return _json({
        "success": True,
        "connection_mode": connection_mode,
        "row_count": len(records),
        "truncated": len(fetched) > safe_limit,
        "max_rows_applied": safe_limit,
        "columns": columns,
        "rows": records,
    })


# ==============================================================================
# Tool registration: sync implementations run in worker threads so a long dbt run
# never blocks the MCP event loop (pings, cancellations, other clients' requests).
# The plain functions above stay importable for tests.
# ==============================================================================
def _register_tool(fn: Callable[..., str]) -> None:
    @functools.wraps(fn)
    async def _async_tool(*args: Any, **kwargs: Any) -> str:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    mcp.tool()(_async_tool)


for _tool_fn in (
    create_staging_model_from_lake,
    create_dim_date_model,
    generate_dbt_schema_tests,
    get_dbt_metadata,
    compile_dbt_model,
    run_dbt_models,
    run_dbt_tests,
    list_duckdb_tables,
    list_data_lake_files,
    query_duckdb,
):
    _register_tool(_tool_fn)


def main() -> None:
    if not (DBT_PROJECT_DIR / "dbt_project.yml").is_file():
        logger.error(
            "dbt_project.yml not found in DBT_PROJECT_DIR=%s. Set DBT_PROJECT_DIR in the MCP config.",
            DBT_PROJECT_DIR,
        )
        sys.exit(2)
    _ensure_project_cwd()
    logger.info(
        "Starting MCP server: project=%s duckdb=%s lake=%s dbt=%s duckdb-lib=%s",
        DBT_PROJECT_DIR, DUCKDB_PATH, DATA_LAKE_DIR, DBT_BIN, duckdb.__version__,
    )
    mcp.run()


if __name__ == "__main__":
    main()
