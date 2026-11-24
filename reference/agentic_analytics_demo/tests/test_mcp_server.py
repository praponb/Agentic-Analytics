"""Verification suite for the dbt + DuckDB MCP server.

Runs against a throw-away copy of the project in a temp directory, so it never
overwrites models, YAML, or warehouse.duckdb in the real checkout.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ITEMS = [
    "dbt_project.yml",
    "profiles.yml",
    "generate_mock_lake.py",
    "macros",
    "mcp_server",
    "starter_marts",
]
QUERY_TIMEOUT_SEC = 3


def _j(raw: str) -> dict:
    return json.loads(raw)


# ------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def project(tmp_path_factory) -> Path:
    root = (tmp_path_factory.mktemp("aa") / "agentic_analytics_demo").resolve()
    root.mkdir()
    for item in PROJECT_ITEMS:
        src = REPO_ROOT / item
        if src.is_dir():
            shutil.copytree(src, root / item, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, root / item)
    (root / "models" / "staging").mkdir(parents=True)
    (root / "models" / "marts").mkdir(parents=True)

    # Bait files the SQL sandbox must never expose
    (root / ".env").write_text("API_KEY=sk-test-secret\n", encoding="utf-8")
    (root.parent / "outside_secret.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root.parent / "outside_db").mkdir()
    (root.parent / "outside_db" / "schema.sql").write_text("SELECT 'leaked-secret';", encoding="utf-8")
    (root / "data_lake_secret").mkdir()
    (root / "data_lake_secret" / "x.csv").write_text("a\n1\n", encoding="utf-8")

    # Generator must work from any CWD
    subprocess.run([sys.executable, str(root / "generate_mock_lake.py")], check=True, cwd="/")
    return root


@pytest.fixture(scope="session")
def m(project):
    os.environ.update({
        "DBT_PROJECT_DIR": str(project),
        "DBT_PROFILES_DIR": str(project),
        "DUCKDB_PATH": str(project / "warehouse.duckdb"),
        "DATA_LAKE_DIR": str(project / "data_lake"),
        "MCP_QUERY_TIMEOUT_SEC": str(QUERY_TIMEOUT_SEC),
    })
    dbt_bin = Path(sys.executable).parent / "dbt"
    if dbt_bin.exists():
        os.environ["DBT_BIN"] = str(dbt_bin)
    sys.path.insert(0, str(project / "mcp_server"))
    import dbt_mcp_server

    return dbt_mcp_server


@pytest.fixture(scope="session")
def built(m, project):
    """Run the documented workflow once: discover -> scaffold -> dim_date -> marts -> run -> profile."""
    lake = _j(m.list_data_lake_files())
    assert lake["total_files"] == 2, lake
    assert {f["query_path"] for f in lake["files"]} == {
        "data_lake/raw_sales_2026.parquet",
        "data_lake/sales_targets.csv",
    }

    s1 = _j(m.create_staging_model_from_lake("raw_sales_2026.parquet"))
    assert s1["success"] and s1["dbt_parse_valid"], s1
    s2 = _j(m.create_staging_model_from_lake("data_lake/sales_targets.csv"))
    assert s2["success"] and s2["dbt_parse_valid"], s2

    dd = _j(m.create_dim_date_model("2025-01-01", "2027-12-31", 10))
    assert dd["success"] and dd["dbt_parse_valid"], dd

    for f in (project / "starter_marts").iterdir():
        shutil.copy2(f, project / "models" / "marts" / f.name)

    run_res = _j(m.run_dbt_models())
    assert run_res["success"], run_res
    assert all(r["status"] == "success" for r in run_res["node_results"]), run_res

    profile = _j(m.generate_dbt_schema_tests("stg_data_lake__raw_sales_2026"))
    assert profile["success"] and profile["dbt_parse_valid"], profile
    return {"profile": profile}


# ------------------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------------------
def test_dbt_tests_pass(m, built):
    res = _j(m.run_dbt_tests())
    assert res["success"] and res["failed_count"] == 0, res
    assert res["total_tests"] >= 40


def test_fy2026_actual_vs_target(m, built):
    q = _j(m.query_duckdb(
        """
        SELECT
            REPLACE(r.rep_name, ' ', '_') AS rep_clean,
            d.fiscal_year_label,
            SUM(f.amount) AS actual_sales,
            t.annual_target
        FROM marts.fact_sales f
        JOIN marts.dim_date d ON f.date_key = d.date_key
        JOIN marts.dim_sales_rep r ON f.rep_id = r.rep_id
        JOIN 'data_lake/sales_targets.csv' t
          ON f.rep_id = t.rep_id AND d.fiscal_year = t.fiscal_year
        WHERE d.fiscal_year = 2026
        GROUP BY 1, 2, 4
        ORDER BY actual_sales DESC
        """
    ))
    assert q["success"] and q["row_count"] == 4, q


def test_fiscal_year_boundaries(m, built):
    q = _j(m.query_duckdb(
        "SELECT date_day::varchar AS d, fiscal_year, fiscal_quarter, fiscal_month_number "
        "FROM marts.dim_date WHERE date_day IN ('2025-09-30', '2025-10-01', '2026-01-01') ORDER BY 1"
    ))
    assert [(r["fiscal_year"], r["fiscal_quarter"], r["fiscal_month_number"]) for r in q["rows"]] == [
        (2025, 4, 12), (2026, 1, 1), (2026, 2, 4)
    ], q


def test_staging_view_resolves_relative_lake_path(m, built):
    q = _j(m.query_duckdb("SELECT count(*) AS n FROM staging.stg_data_lake__raw_sales_2026"))
    assert q["success"] and q["rows"][0]["n"] == 24, q


def test_compile_graph_selector(m, built):
    res = _j(m.compile_dbt_model("+fact_sales"))
    assert res["success"], res
    assert any(uid.endswith(".fact_sales") for uid in res["compiled_sql"]), res
    assert res["compiled_count"] >= 2


def test_metadata_lists_marts(m, built):
    meta = _j(m.get_dbt_metadata("fact_sales"))
    assert meta["success"] and meta["total_models"] >= 1
    assert meta["models"][0]["properties_file"].endswith("marts_models.yml")


# ------------------------------------------------------------------------------
# Test inference & YAML merge
# ------------------------------------------------------------------------------
def _col_tests(project: Path, rel_yml: str, model: str, column: str) -> dict:
    doc = yaml.safe_load((project / rel_yml).read_text(encoding="utf-8"))
    mdl = next(x for x in doc["models"] if x["name"] == model)
    return next(c for c in mdl["columns"] if c["name"] == column)


def test_inferred_tests_not_overfitted(project, built):
    yml = "models/staging/schema.yml"
    sale_date = _col_tests(project, yml, "stg_data_lake__raw_sales_2026", "sale_date")
    order_id = _col_tests(project, yml, "stg_data_lake__raw_sales_2026", "order_id")
    region = _col_tests(project, yml, "stg_data_lake__raw_sales_2026", "region")
    rep_id = _col_tests(project, yml, "stg_data_lake__raw_sales_2026", "rep_id")

    assert "unique" not in sale_date["data_tests"]          # dates are distinct only by coincidence
    assert "unique" in order_id["data_tests"]                # key-like name
    av = next(t for t in region["data_tests"] if isinstance(t, dict))
    assert av["accepted_values"]["config"]["severity"] == "warn"
    assert not any(isinstance(t, dict) for t in rep_id["data_tests"])  # keys get no accepted_values


def test_reprofile_preserves_manual_edits(m, project, built):
    yml_path = project / "models/staging/schema.yml"
    doc = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
    mdl = next(x for x in doc["models"] if x["name"] == "stg_data_lake__raw_sales_2026")
    col = next(c for c in mdl["columns"] if c["name"] == "sale_date")
    col["description"] = "Hand-written: order booking date"
    col["meta"] = {"owner": "finance"}
    col["data_tests"] = ["not_null", {"relationships": {"to": "ref('dim_date')", "field": "date_day"}}]
    yml_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    res = _j(m.generate_dbt_schema_tests("stg_data_lake__raw_sales_2026"))
    assert res["success"], res

    col = _col_tests(project, "models/staging/schema.yml", "stg_data_lake__raw_sales_2026", "sale_date")
    assert col["description"] == "Hand-written: order booking date"
    assert col["meta"] == {"owner": "finance"}
    assert {"relationships": {"to": "ref('dim_date')", "field": "date_day"}} in col["data_tests"]
    assert _j(m.run_dbt_tests("stg_data_lake__raw_sales_2026"))["failed_count"] == 0


def test_profiling_marts_model_writes_to_existing_yaml(m, project, built):
    res = _j(m.generate_dbt_schema_tests("fact_sales", dry_run=True))
    assert res["success"] and res["dry_run"], res
    assert res["target_yaml_file"].endswith("marts_models.yml")


# ------------------------------------------------------------------------------
# Read-only SQL & sandbox
# ------------------------------------------------------------------------------
def test_sandbox_is_active(m, built):
    q = _j(m.query_duckdb("SELECT current_setting('enable_external_access') AS ext"))
    assert q["success"] and q["rows"][0]["ext"] is False, q


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_text('.env')",
        "SELECT * FROM read_text('profiles.yml')",
        "SELECT * FROM read_text('~/.zshrc')",
        "SELECT * FROM read_text('data_lake/../.env')",
        "SELECT * FROM 'data_lake_secret/x.csv'",
        'SELECT * FROM "{parent}/outside_secret.csv"',
        "SELECT * FROM '{parent}/outside_secret.csv'",
        "SELECT * FROM read_parquet(['data_lake/raw_sales_2026.parquet', '/etc/passwd'])",
    ],
)
def test_file_reads_outside_lake_blocked(m, project, built, sql):
    res = _j(m.query_duckdb(sql.format(parent=project.parent)))
    assert not res["success"], res
    assert "Permission" in res["error"] or "disabled by configuration" in res["error"], res


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE marts.dim_date",
        "COPY (SELECT 1) TO 'data_lake/x.csv'",
        "ATTACH 'other.duckdb'",
        "SET threads = 1",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA enable_profiling",
        "PRAGMA force_checkpoint",
        "CALL pragma_version()",
        "SELECT 1; SELECT 2",
        "SELECT E'\\'' AS a, 1 ; SELECT 'x' AS b",
        "CREATE TABLE t AS SELECT 1",
    ],
)
def test_non_readonly_statements_rejected(m, built, sql):
    res = _j(m.query_duckdb(sql))
    assert not res["success"], res


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'drop; update' AS safe_text",
        "FROM marts.dim_date LIMIT 1",
        "SELECT 1 AS reset_flag, 2 AS \"set\"",
        "SHOW TABLES",
        "DESCRIBE marts.fact_sales",
        "SUMMARIZE marts.fact_sales",
        "SELECT * FROM 'data_lake/sales_targets.csv'",
        "SELECT count(*) AS n FROM glob('data_lake/*')",
        "EXPLAIN SELECT 1",
        "SELECT 1;",
    ],
)
def test_readonly_statements_allowed(m, built, sql):
    res = _j(m.query_duckdb(sql))
    assert res["success"], res


def test_parse_time_file_reads_blocked(m, project, built):
    res = _j(m.query_duckdb(f"PRAGMA import_database('{project.parent}/outside_db')"))
    assert not res["success"], res
    assert "leaked-secret" not in json.dumps(res), res


def test_query_timeout(m, built):
    res = _j(m.query_duckdb("SELECT sum(hash(i)) AS h FROM range(1000000000000) t(i)"))
    assert not res["success"] and res.get("timed_out") is True, res


# ------------------------------------------------------------------------------
# dbt CLI safety
# ------------------------------------------------------------------------------
def test_selector_flag_injection_blocked(m):
    assert not _j(m.run_dbt_models(select="--help"))["success"]
    assert not _j(m.run_dbt_tests(select="model --target prod"))["success"]


def test_failed_dbt_run_reports_no_stale_results(m, project, built):
    assert _j(m.run_dbt_tests())["success"]
    broken = project / "models" / "marts" / "broken.sql"
    broken.write_text("select * from {{ ref('does_not_exist') }}", encoding="utf-8")
    try:
        res = _j(m.run_dbt_tests())
        assert not res["success"] and res["total_tests"] == 0 and "logs" in res, res
        run = _j(m.run_dbt_models())
        assert not run["success"] and run["node_results"] == [], run
    finally:
        broken.unlink()
        _j(m.get_dbt_metadata(force_refresh=True))


def test_cross_process_lock_reported(m, project, built):
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys, time; "
            f"c = duckdb.connect({str(project / 'warehouse.duckdb')!r}, read_only=True); "
            "print('ready', flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        res = _j(m.run_dbt_models(select="dim_date"))
        assert not res["success"] and res.get("lock_conflict") is True, res
    finally:
        holder.kill()
        holder.wait()


# ------------------------------------------------------------------------------
# MCP protocol over STDIO (real client, server launched from CWD "/")
# ------------------------------------------------------------------------------
def test_stdio_protocol_from_foreign_cwd(project, built):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(project / "mcp_server" / "dbt_mcp_server.py")],
            cwd="/",
            # Only PATH/HOME: the server must find its project from its own file location.
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/")},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                result = await session.call_tool(
                    "query_duckdb",
                    {"sql": "SELECT count(*) AS n FROM staging.stg_data_lake__raw_sales_2026"},
                )
                return tools, json.loads(result.content[0].text)

    tools, payload = asyncio.run(scenario())
    assert {
        "create_staging_model_from_lake", "create_dim_date_model", "generate_dbt_schema_tests",
        "get_dbt_metadata", "compile_dbt_model", "run_dbt_models", "run_dbt_tests",
        "list_duckdb_tables", "list_data_lake_files", "query_duckdb",
    } <= tools
    assert payload["success"] and payload["rows"][0]["n"] == 24, payload
