# Agentic Analytics Open-Source Blueprint (dbt + DuckDB + FastMCP)

**Claude Code Implementation Specification — v1.1 (corrected & verified edition, 2026-11-24)**

> **How to use this document.** Hand this Markdown file (not the v1.0 `.docx`) to Claude Code.
> Every code block below is copied by script from a project that was installed and tested, so
> copy the code exactly. A tested reference copy of every file sits next to this spec in
> `reference/agentic_analytics_demo/`.
>
> **Verification status**
>
> | Part | Status |
> |---|---|
> | Local core (dbt + DuckDB + MCP server, Sections 4–6) | **Tested.** `pytest` 47/47 on DuckDB 2.0.1, 1.4.4 and 1.3.2 with Python 3.12.13, and on Python 3.11.15 (dbt-core 1.12.7, dbt-duckdb 1.11.1, mcp 1.30.0), locally and in GitHub Actions; `dbt build` 66/66; MCP protocol tested over STDIO with a real client. |
> | Execution checklist (Section 10) | **Tested** end to end in a fresh copy. |
> | Enterprise Docker stack (Section 7) | **Tested live** on Docker 29.9.3 (Postgres 16.16, Cube v1.8.32 + Cube Store, Metabase v0.65.6.3), from empty volumes, with `CUBEJS_DEV_MODE` both `true` and `false`: marts published to Postgres; RLS checked over Cube REST (JWT) and the SQL API; Metabase connected to the SQL API with native SQL and GUI questions filtered per user. `scripts/check_enterprise_stack.py` reproduces these checks. |
> | Sales dashboard + hosting (Section 11, optional add-on) | **Tested.** 8 `pytest` tests (Streamlit 1.67, AppTest); checked in Chrome in light and dark mode; image built with `dbt build` 66/66 (runtime image holds only Streamlit + DuckDB) and run on Ubuntu 26.04 (Docker 29) behind a Cloudflare Tunnel and Cloudflare Access; container image build and health check run in GitHub Actions. |

---

## 0. What changed from v1.0

| # | v1.0 problem | v1.1 fix |
|---|---|---|
| 1 | `mcp[cli]>=1.2.0` resolves to MCP SDK 2.x, which removed `mcp.server.fastmcp` → server cannot import. | Pinned `mcp[cli]>=1.2.0,<2.0.0`. |
| 2 | `uv pip install -e ".[dev]"` fails: *Multiple top-level packages discovered in a flat-layout*. | `[tool.uv] package = false`, dev deps in `[dependency-groups]`, install with `uv sync`, commit `uv.lock`. |
| 3 | The `.docx` has invisible U+EC02/U+EC03 characters around every code block → `SyntaxError` when pasted. | Spec delivered as Markdown, generated from source files; checked to contain no private-use characters. |
| 4 | Sandbox failed **open**: `duckdb>=1.1.0` allows versions without `allowed_directories`; the error was logged at DEBUG level and queries ran unsandboxed (a `.env` file was readable). | `duckdb>=1.3.0`; every DuckDB connection goes through `_connect()`, which applies **and verifies** the sandbox, and refuses to run if it can't (fail closed). |
| 5 | Regex SQL checker was bypassable (`read_text('.env')`, `'~/…'`, double-quoted paths, `E'\''` stacked queries) and blocked valid SQL (`FROM tbl`, `AS "set"`). | Regex replaced by DuckDB's own parser (`extract_statements`): exactly one SELECT-family or EXPLAIN statement. The engine sandbox is the file boundary. The parser itself runs sandboxed (DuckDB 1.x reads files while parsing `PRAGMA import_database`). |
| 6 | Failed dbt runs reported the **previous** run's `run_results.json` as current results. | `run_results.json` is deleted before each dbt command and read back inside the same lock. |
| 7 | Re-profiling replaced a model's column list, deleting hand-written tests (`relationships`), `meta`, constraints. | Per-column merge: existing keys and tests are kept, an inferred test is only added if no test of the same kind exists, missing columns are kept and reported as `stale_columns`; writes into the YAML file that already documents the model. |
| 8 | Inferred tests overfit sample data (`unique` on `sale_date`; hard-failing `accepted_values` lists). | `unique` only for key-like names (`id`, `*_id`, `*_key`, `*_code`) and never for date/time/float types; `accepted_values` emitted with `severity: warn`; `dry_run` option; warnings don't fail `run_dbt_tests`. |
| 9 | `threading.Lock` only protects one process; DuckDB file-lock conflicts with other processes surfaced as raw errors; sync tools blocked the MCP event loop; no query timeout. | Tools run in worker threads (`anyio.to_thread`); lock conflicts return `lock_conflict: true` + hint; `query_duckdb` is cancelled after `MCP_QUERY_TIMEOUT_SEC`; per-connection `memory_limit` / `threads`. |
| 10 | Enterprise stack had no data path: nothing loaded Postgres. | `scripts/publish_marts_to_postgres.py` copies every `marts` table from DuckDB to Postgres. |
| 11 | Cube model referenced an undefined `fact_sales` model, undefined `SalesReps` / `Courses` cubes and a non-existent `course_id`; "revenue after discounts" had no discount data. | Starter marts (`fact_sales`, `dim_sales_rep`, `dim_course`, `fact_sales_target`) with relationship tests; all four cubes defined; measure descriptions match the data. |
| 12 | Row-level security was promised (Sections 1–2) but not implemented anywhere. | Cube `queryRewrite` + `checkSqlAuth` RLS by `rep_id`. Docs state plainly that RLS applies only through Cube; the MCP query tool is a trusted-analyst tool with full read access. |
| 13 | Docker: SQL API port without `CUBEJS_PG_SQL_PORT`, hardcoded passwords, `:latest` tags, ports on all interfaces, init SQL that only runs on fresh volumes. | `CUBEJS_PG_SQL_PORT` set; secrets from `docker/.env`; pinned image tags; ports bound to `127.0.0.1`; idempotent `db-init` service. |
| 14 | Tests overwrote the real project (`overwrite=True`); security tests only exercised the regex; dbt and pytest shared `tests/`; `DBT_PROJECT_DIR` silently defaulted to the CWD; unbounded Python version; fiscal-year naming assumed. | Tests run in a temp copy and cover every bypass; dbt uses `data_tests/`; project dir defaults to the server file's location and startup fails if `dbt_project.yml` is missing; Python `>=3.11,<3.13` + `.python-version`; `fiscal_year_naming` parameter (`end`/`start`). |

**Found when the Docker stack was run live** (these affected the first v1.1 draft, which had only been checked statically):

- `cube.js` crashed (`TypeError: m.split is not a function`) on SQL API queries that Cube turns into *member expressions*: `count(*)`, functions such as `upper(rep_id)`, subqueries, WHERE on an expression. Metabase GUI questions produce these. The query failed (no data leaked), but dashboards broke. `queryRewrite` now reads the cubes used from both member strings and member-expression objects, and over-filters rather than skips anything it does not recognise. Covered by `docker/cube_rls_unit_check.js` and `scripts/check_enterprise_stack.py`.
- With `CUBEJS_DEV_MODE=false` every query failed: production mode needs a separate Cube Store, which the compose file lacked. Added a pinned `cubestore` service; it also stops dev mode from writing an embedded Cube Store into `docker/cube_schema/.cubestore` (inside the repo).
- In dev mode Cube does not reject invalid or unsigned (`alg: none`) JWTs; it passes an empty security context. `cube.js` denies any query without `role: admin` or `rep_id`, which is what keeps dev mode safe. Keep that deny-by-default rule if you change `cube.js`.

**Found while implementing from this spec** (Section 10 run end to end in a fresh folder, including step 7):

- `query_duckdb` returned DECIMAL results (for example `SUM(amount)`) as JSON strings (`"414000.0"`) while DOUBLE columns came back as numbers. DECIMALs now become JSON numbers whenever a float holds the value exactly; values that would lose digits (for example a `DECIMAL(38,2)` total) stay exact strings. Covered by `test_decimal_results_are_json_numbers`.
- `publish_marts_to_postgres.py` printed the full Postgres connection string, **password included**, in its traceback when Postgres was unreachable (DuckDB echoes the DSN in the error). Connection and publish errors are now reported in one line with passwords masked (`password=***`, `postgresql://user:***@host`).

**Added after implementation** (optional, Section 11):

- A small Streamlit **sales dashboard** (`dashboard/`) that reads the dbt marts read-only: KPIs, actual vs. target by rep, revenue by fiscal month, breakdowns by course, region and channel, and the order lines. It has no row-level security, so it listens on `127.0.0.1` locally.
- **Hosting** for it: a Docker image whose build runs `dbt build` (a failing dbt test fails the deploy), a Compose project with a dedicated Cloudflare Tunnel (no inbound ports), and a Cloudflare Access login in front of the hostname.

Additional hardening found while fixing: the dbt subprocess no longer inherits the MCP server's stdin (`stdin=DEVNULL`); DuckDB's progress bar is disabled so nothing can reach stdout; a `DATA_LAKE_DIR` equal to (or a parent of) the project directory is refused; lake file names containing `'`, `{` or `}` are rejected.

---

## 1. Executive Overview & Source Context

This specification consolidates an Agentic Analytics architecture inspired by the walkthrough *"Agentic Analytics: Power BI and Data Work in the Era of Autonomous AI"* (9Expert, https://www.youtube.com/watch?v=0-q6pbosRko), re-engineered to run on free and open-source software on macOS, Linux or Docker, without Microsoft 365, Power BI or Microsoft Fabric licences.

**Core concept from the reference walkthrough**

- **Formula:** Agentic Analytics = Data × AI + Automation.
- **Protocol bridge:** the Model Context Protocol (MCP) lets an AI client (Claude Code, Claude Desktop, VS Code + Cline/Continue) work with both the data layer and the modelling layer.
- **Workflow 1 — Data agent / chat with data** (replaces Microsoft Fabric data agents): discover lake files, query the warehouse and lake together, answer business questions.
- **Workflow 2 — AI-assisted data modelling** (replaces the Power BI modelling MCP): scaffold staging models, generate a date dimension with fiscal-year logic, build a star schema, propose data-quality tests and validate with dbt.

**Governance principle (corrected).** AI works on top of a governed dbt pipeline. Two boundaries exist and must not be confused:

1. **MCP server (`query_duckdb`)** — a *trusted-analyst* tool. Read-only, single statement, file access sandboxed to `data_lake/`, but it can read **all** rows of the warehouse and lake. It has no row-level security.
2. **Cube semantic layer (optional enterprise track)** — the place where row-level security is enforced, for dashboards and less-trusted consumers. RLS applies only to queries that go through Cube.

---

## 2. Architecture

| Capability | Microsoft stack (paid) | Open-source replacement | Role |
|---|---|---|---|
| Lakehouse / storage | Fabric OneLake | DuckDB + Parquet/CSV (local); PostgreSQL (Docker, for BI) | Local OLAP storage and lake querying |
| Transformation & star schema | Fabric notebooks / dataflows | dbt Core + dbt-duckdb | Staging → marts, lineage, data tests |
| Semantic layer & ontology | Fabric IQ ontology + Power BI semantic model | dbt YAML docs + Cube Core | Glossary, governed metrics, joins, **RLS (Cube only)** |
| Data agent & modelling MCP | Fabric IQ MCP + Power BI modelling MCP | Python FastMCP server `mcp_server/dbt_mcp_server.py` | Discovery, scaffolding, dim_date, test inference, dbt run/test/compile, read-only SQL |
| AI client | M365 Copilot / Teams | Claude Code, Claude Desktop, VS Code (Cline/Continue) | Natural-language orchestration |
| BI & access control | Power BI Service | Metabase OSS (via Cube SQL API) | Dashboards |
| Quick report (optional) | Power BI report | Streamlit app `dashboard/` (Section 11) | One trusted viewer; no RLS, so local or behind a login |

```text
[data_lake/*.parquet, *.csv]
          │  read_parquet / read_csv_auto (relative paths: data_lake/...)
          ▼
┌───────────────────────────────────────────────────────────────┐
│ 1. DuckDB (warehouse.duckdb) + dbt-duckdb                     │
│    staging (views) ─► marts (tables): dim_date, dim_sales_rep,│
│    dim_course, fact_sales, fact_sales_target                  │
└──────────────┬───────────────────────────────┬────────────────┘
               │ read-only, sandboxed          │ scripts/publish_marts_to_postgres.py
               ▼                               ▼ (optional enterprise track)
┌──────────────────────────────────┐   ┌──────────────────────────────────────┐
│ 2. MCP server (STDIO JSON-RPC)   │   │ 4. PostgreSQL `warehouse.marts`      │
│  discovery: list_data_lake_files,│   │        ▼                             │
│   list_duckdb_tables, query_duckdb│  │    Cube Core (metrics + RLS)         │
│  modelling: create_staging_…,    │   │        ▼  SQL API :15432 / REST :4000│
│   create_dim_date_model,         │   │    Metabase OSS (dashboards)         │
│   generate_dbt_schema_tests,     │   └──────────────────────────────────────┘
│   get_dbt_metadata, compile_…,   │
│   run_dbt_models, run_dbt_tests  │
└──────────────┬───────────────────┘
               ▼
┌──────────────────────────────────┐
│ 3. AI client: Claude Code /      │
│    Claude Desktop / VS Code      │
└──────────────────────────────────┘
```

---

## 3. Security & concurrency model (Claude Code MUST preserve these safeguards)

| Safeguard | Where | Why |
|---|---|---|
| Every DuckDB connection is opened via `_connect()`: `allowed_directories = [<abs lake>/, data_lake/]`, `enable_external_access = false`, `lock_configuration = true`, then verified. Failure → refuse. | `_connect` | The engine is the only reliable file boundary. Both the absolute and project-relative lake paths are needed because dbt views use `read_parquet('data_lake/…')`. Verified on DuckDB 1.3.2–2.0.1: `.env`, `~`, `data_lake/../`, sibling `data_lake_x/` and absolute outside paths are blocked. DuckDB 1.2.x blocks legitimate relative reads, and 1.1 lacks the setting, hence `duckdb>=1.3.0`. |
| One statement, type SELECT or EXPLAIN, classified by DuckDB's parser on a sandboxed connection. | `_validate_readonly_sql` | Regex parsing of SQL is bypassable. SHOW / DESCRIBE / SUMMARIZE / VALUES / FROM-first all parse as SELECT. |
| Warehouse opened `read_only=True`. | `_connect(DUCKDB_PATH)` | No writes through the query tool. |
| Query cancelled after `MCP_QUERY_TIMEOUT_SEC` (default 60); `memory_limit` / `threads` per connection. | `query_duckdb` | A runaway query would otherwise hold the lock indefinitely. |
| `_DB_LOCK` serialises dbt runs and DuckDB connections in-process; tools run in worker threads. | `_execute_dbt_cli`, `_register_tool` | Parallel tool calls; MCP event loop stays responsive during long dbt runs. |
| Cross-process DuckDB lock conflicts → `lock_conflict: true` + hint. | `_error_payload`, `_execute_dbt_cli` | DuckDB allows one writer *or* many readers across processes (DuckDB CLI, DBeaver, a second MCP server). |
| `run_results.json` deleted before each dbt command, read inside the lock. | `_execute_dbt_cli` | No stale results after a failed run. |
| dbt selectors split into tokens; tokens starting with `-` rejected; no shell. | `_parse_dbt_selectors` | CLI flag injection. |
| Nothing writes to stdout: logging → stderr, subprocess `capture_output=True` + `stdin=DEVNULL`, DuckDB progress bar off. | module-wide | STDIO JSON-RPC transport integrity. |
| `DBT_PROJECT_DIR` defaults to the server file's parent directory; startup exits if `dbt_project.yml` is missing; CWD pinned to the project. | `main`, `_ensure_project_cwd` | MCP clients may launch servers from `/` or `$HOME`. |

---

## 4. Project structure & dependencies

```text
agentic_analytics_demo/
├── pyproject.toml
├── uv.lock                      # generated by `uv sync`; commit it
├── .python-version
├── .gitignore
├── dbt_project.yml
├── profiles.yml
├── generate_mock_lake.py
├── macros/
│   └── generate_schema_name.sql
├── models/
│   ├── staging/                 # created by create_staging_model_from_lake
│   └── marts/                   # dim_date (tool) + starter marts (copied in step 6 of Section 10)
├── starter_marts/               # star-schema models; copied into models/marts AFTER staging exists
│   ├── dim_sales_rep.sql
│   ├── dim_course.sql
│   ├── fact_sales.sql
│   ├── fact_sales_target.sql
│   └── marts_models.yml
├── data_tests/                  # dbt singular tests (empty; pytest owns tests/)
├── data_lake/                   # generated by generate_mock_lake.py (git-ignored)
├── mcp_server/
│   └── dbt_mcp_server.py
├── scripts/
│   ├── write_mcp_config.py
│   ├── publish_marts_to_postgres.py
│   └── check_enterprise_stack.py  # live smoke test of the Docker stack (Section 7)
├── tests/
│   └── test_mcp_server.py
├── docker/                      # optional enterprise stack
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── cube_rls_unit_check.js   # offline unit checks of cube.js (Node, no Cube needed)
│   └── cube_schema/
│       ├── cube.js
│       └── model/
│           └── sales.yml
└── .mcp.json.example            # .mcp.json itself is generated per machine (git-ignored)
```

> **Why `starter_marts/` is outside `models/`:** the marts `ref()` the staging models that the MCP tools generate. If they sat in `models/marts` from the start, the very first `dbt parse` (run by the first scaffolding call) would fail on missing refs.

### pyproject.toml

**`pyproject.toml`**

```toml
[project]
name = "agentic-analytics-demo"
version = "1.1.0"
description = "Open-source Agentic Analytics pipeline with dbt, DuckDB, and FastMCP"
# Verified on Python 3.12. Widen only after running the test suite on the new version.
requires-python = ">=3.11,<3.13"
dependencies = [
    "dbt-core>=1.8.0,<2.0.0",
    "dbt-duckdb>=1.8.0,<2.0.0",
    # >=1.3: allowed_directories sandbox works with relative data_lake/ paths (1.1 lacks it, 1.2 blocks valid reads)
    "duckdb>=1.3.0,<3.0.0",
    # mcp 2.x removed mcp.server.fastmcp (renamed to MCPServer); this server targets the 1.x API
    "mcp[cli]>=1.2.0,<2.0.0",
    "pyyaml>=6.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0.0",
]

[tool.uv]
# This is an application, not an installable package (avoids setuptools flat-layout errors).
package = false

[tool.pytest.ini_options]
testpaths = ["tests"]
```

### .python-version

**`.python-version`**

```text
3.12
```

### .gitignore

**`.gitignore`**

```text
.venv/
__pycache__/
.pytest_cache/
target/
dbt_packages/
logs/
warehouse.duckdb
warehouse.duckdb.wal
data_lake/
.env
docker/.env
.mcp.json
docker/cube_schema/.cubestore/
.user.yml
```

---

## 5. dbt project files

### dbt_project.yml

**`dbt_project.yml`**

```yaml
name: 'agentic_analytics_demo'
version: '1.1.0'
config-version: 2

profile: 'agentic_analytics_demo'

model-paths: ["models"]
analysis-paths: ["analyses"]
# dbt singular tests live in data_tests/; pytest owns tests/
test-paths: ["data_tests"]
seed-paths: ["seeds"]
macro-paths: ["macros"]
snapshot-paths: ["snapshots"]

clean-targets:
  - "target"
  - "dbt_packages"

models:
  agentic_analytics_demo:
    staging:
      +schema: staging
      +materialized: view
    marts:
      +schema: marts
      +materialized: table
```

### profiles.yml

**`profiles.yml`**

```yaml
agentic_analytics_demo:
  target: dev
  outputs:
    dev:
      type: duckdb
      # The MCP server always injects an absolute DUCKDB_PATH. The relative default only
      # applies when you run dbt by hand, so run it from the project root.
      path: "{{ env_var('DUCKDB_PATH', 'warehouse.duckdb') }}"
      threads: 4
      settings:
        memory_limit: '2GB'
```

### macros/generate_schema_name.sql

Produces clean schema names (`staging`, `marts`) instead of dbt's default `main_staging` / `main_marts`.

**`macros/generate_schema_name.sql`**

```sql
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set default_schema = target.schema -%}
    {%- if custom_schema_name is none -%}
        {{ default_schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
```

### generate_mock_lake.py

Generates `data_lake/raw_sales_2026.parquet` (with a spaced column name, `"Order ID"`, to exercise snake_case normalisation; 24 rows spanning FY2025–FY2027 for an October fiscal start) and `data_lake/sales_targets.csv` (FY2026 and FY2027 targets). DuckDB only — no pandas/pyarrow. Works from any CWD.

**`generate_mock_lake.py`**

```python
from pathlib import Path
import duckdb


def _sql_path(path: Path) -> str:
    """Single-quoted SQL literal for a file path (handles quotes in directory names)."""
    return "'" + path.resolve().as_posix().replace("'", "''") + "'"


def main() -> None:
    lake_dir = Path(__file__).resolve().parent / "data_lake"
    lake_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = lake_dir / "raw_sales_2026.parquet"
    csv_path = lake_dir / "sales_targets.csv"

    con = duckdb.connect(":memory:")
    try:
        # 1. Mock sales transactions (24 rows spanning FY2025, FY2026, and FY2027)
        con.execute(
            f"""
            COPY (
                SELECT * FROM (VALUES
                    -- Late FY2025 (Sep 2025)
                    ('ORD-2025-001', DATE '2025-09-15', 'REP01', 'Boonsom', 'Bangkok',   'Power BI Essentials', 'Public',   5,  6500.0,  32500.0),
                    ('ORD-2025-002', DATE '2025-09-28', 'REP02', 'Wipa',    'Central',   'Python for Data',     'In-house', 1, 45000.0,  45000.0),

                    -- FY2026 Q1 (Oct - Dec 2025)
                    ('ORD-2026-001', DATE '2025-10-02', 'REP01', 'Boonsom', 'Bangkok',   'Microsoft Fabric',    'In-house', 1, 65000.0,  65000.0),
                    ('ORD-2026-002', DATE '2025-10-18', 'REP03', 'Somchai', 'North',     'Excel Advanced',      'Public',   8,  4500.0,  36000.0),
                    ('ORD-2026-003', DATE '2025-11-05', 'REP02', 'Wipa',    'Bangkok',   'AI & Copilot',        'Hybrid',   4,  8900.0,  35600.0),
                    ('ORD-2026-004', DATE '2025-11-22', 'REP01', 'Boonsom', 'East',      'Power BI Essentials', 'In-house', 1, 55000.0,  55000.0),
                    ('ORD-2026-005', DATE '2025-12-10', 'REP04', 'Napa',    'South',     'Digital Marketing',   'Public',   6,  5900.0,  35400.0),
                    ('ORD-2026-006', DATE '2025-12-19', 'REP01', 'Boonsom', 'Bangkok',   'AI & Copilot',        'In-house', 1, 75000.0,  75000.0),

                    -- FY2026 Q2 - Q4 (Jan - Sep 2026)
                    ('ORD-2026-007', DATE '2026-01-14', 'REP02', 'Wipa',    'Central',   'Microsoft Fabric',    'Public',   4,  9500.0,  38000.0),
                    ('ORD-2026-008', DATE '2026-02-20', 'REP03', 'Somchai', 'North',     'Power BI Essentials', 'Hybrid',   5,  6500.0,  32500.0),
                    ('ORD-2026-009', DATE '2026-03-11', 'REP01', 'Boonsom', 'Bangkok',   'Python for Data',     'In-house', 1, 60000.0,  60000.0),
                    ('ORD-2026-010', DATE '2026-04-24', 'REP04', 'Napa',    'South',     'Excel Advanced',      'Public',   7,  4500.0,  31500.0),
                    ('ORD-2026-011', DATE '2026-05-15', 'REP01', 'Boonsom', 'East',      'Microsoft Fabric',    'In-house', 1, 70000.0,  70000.0),
                    ('ORD-2026-012', DATE '2026-06-09', 'REP02', 'Wipa',    'Bangkok',   'Digital Marketing',   'Hybrid',   6,  5900.0,  35400.0),
                    ('ORD-2026-013', DATE '2026-07-17', 'REP03', 'Somchai', 'Northeast', 'Power BI Essentials', 'In-house', 1, 52000.0,  52000.0),
                    ('ORD-2026-014', DATE '2026-08-21', 'REP01', 'Boonsom', 'Bangkok',   'AI & Copilot',        'Public',  10,  8900.0,  89000.0),
                    ('ORD-2026-015', DATE '2026-09-29', 'REP02', 'Wipa',    'Central',   'Python for Data',     'In-house', 1, 58000.0,  58000.0),

                    -- FY2027 Q1 (Oct - Nov 2026)
                    ('ORD-2027-001', DATE '2026-10-05', 'REP01', 'Boonsom', 'Bangkok',   'Microsoft Fabric',    'In-house', 1, 80000.0,  80000.0),
                    ('ORD-2027-002', DATE '2026-10-14', 'REP03', 'Somchai', 'North',     'AI & Copilot',        'Hybrid',   5,  8900.0,  44500.0),
                    ('ORD-2027-003', DATE '2026-10-27', 'REP04', 'Napa',    'South',     'Power BI Essentials', 'Public',   6,  6500.0,  39000.0),
                    ('ORD-2027-004', DATE '2026-11-03', 'REP02', 'Wipa',    'Bangkok',   'Microsoft Fabric',    'In-house', 1, 72000.0,  72000.0),
                    ('ORD-2027-005', DATE '2026-11-12', 'REP01', 'Boonsom', 'East',      'AI & Copilot',        'In-house', 1, 85000.0,  85000.0),
                    ('ORD-2027-006', DATE '2026-11-18', 'REP03', 'Somchai', 'Northeast', 'Digital Marketing',   'Public',   8,  5900.0,  47200.0),
                    ('ORD-2027-007', DATE '2026-11-23', 'REP04', 'Napa',    'Bangkok',   'Python for Data',     'Hybrid',   4,  7500.0,  30000.0)
                ) AS t(
                    "Order ID", sale_date, rep_id, rep_name, region,
                    course_name, channel_type, quantity, unit_price, amount
                )
            ) TO {_sql_path(parquet_path)} (FORMAT PARQUET)
            """
        )

        # 2. Mock annual sales targets by fiscal year
        con.execute(
            f"""
            COPY (
                SELECT * FROM (VALUES
                    ('REP01', 'Boonsom', 2026, 350000.0),
                    ('REP02', 'Wipa',    2026, 200000.0),
                    ('REP03', 'Somchai', 2026, 150000.0),
                    ('REP04', 'Napa',    2026, 120000.0),
                    ('REP01', 'Boonsom', 2027, 400000.0),
                    ('REP02', 'Wipa',    2027, 220000.0),
                    ('REP03', 'Somchai', 2027, 180000.0),
                    ('REP04', 'Napa',    2027, 150000.0)
                ) AS t(rep_id, rep_name, fiscal_year, annual_target)
            ) TO {_sql_path(csv_path)} (FORMAT CSV, HEADER, DELIMITER ',')
            """
        )
    finally:
        con.close()

    print(f"Generated mock lake files:\n - {parquet_path}\n - {csv_path}")


if __name__ == "__main__":
    main()
```

### Starter marts (`starter_marts/`)

Star schema at order-line grain. `course_id` is a stable surrogate key (`md5` of the normalised course name).

**`starter_marts/dim_sales_rep.sql`**

```sql
{{ config(materialized='table') }}

-- One row per sales rep, from both transactions and targets
with reps as (

    select rep_id, rep_name from {{ ref('stg_data_lake__raw_sales_2026') }}
    union
    select rep_id, rep_name from {{ ref('stg_data_lake__sales_targets') }}

)

select
    rep_id,
    max(rep_name) as rep_name
from reps
group by rep_id
```

**`starter_marts/dim_course.sql`**

```sql
{{ config(materialized='table') }}

-- Stable surrogate key derived from the normalized course name
select
    md5(lower(trim(course_name))) as course_id,
    min(course_name) as course_name
from {{ ref('stg_data_lake__raw_sales_2026') }}
group by 1
```

**`starter_marts/fact_sales.sql`**

```sql
{{ config(materialized='table') }}

-- Grain: one row per order line
select
    order_id,
    sale_date,
    cast(strftime(sale_date, '%Y%m%d') as integer) as date_key,
    rep_id,
    md5(lower(trim(course_name))) as course_id,
    region,
    channel_type,
    quantity,
    unit_price,
    amount
from {{ ref('stg_data_lake__raw_sales_2026') }}
```

**`starter_marts/fact_sales_target.sql`**

```sql
{{ config(materialized='table') }}

-- Grain: one row per sales rep per fiscal year
select
    rep_id || '-' || fiscal_year::varchar as target_id,
    rep_id,
    fiscal_year::integer as fiscal_year,
    annual_target
from {{ ref('stg_data_lake__sales_targets') }}
```

**`starter_marts/marts_models.yml`**

```yaml
version: 2

models:
  - name: dim_sales_rep
    description: "Sales rep dimension (one row per rep)"
    columns:
      - name: rep_id
        description: "Sales rep business key"
        data_tests: [unique, not_null]
      - name: rep_name
        description: "Sales rep display name"
        data_tests: [not_null]

  - name: dim_course
    description: "Course dimension (one row per course)"
    columns:
      - name: course_id
        description: "md5 of the lower-cased, trimmed course name"
        data_tests: [unique, not_null]
      - name: course_name
        description: "Course display name"
        data_tests: [not_null]

  - name: fact_sales
    description: "Sales transactions (grain: one row per order line). Amounts in THB, no discounts modelled."
    columns:
      - name: order_id
        description: "Order identifier"
        data_tests: [unique, not_null]
      - name: date_key
        description: "FK to dim_date (YYYYMMDD)"
        data_tests:
          - not_null
          - relationships:
              to: ref('dim_date')
              field: date_key
      - name: rep_id
        description: "FK to dim_sales_rep"
        data_tests:
          - not_null
          - relationships:
              to: ref('dim_sales_rep')
              field: rep_id
      - name: course_id
        description: "FK to dim_course"
        data_tests:
          - not_null
          - relationships:
              to: ref('dim_course')
              field: course_id
      - name: amount
        description: "Order line revenue in THB (quantity x unit_price)"
        data_tests: [not_null]

  - name: fact_sales_target
    description: "Annual sales target per rep and fiscal year (THB)"
    columns:
      - name: target_id
        description: "rep_id-fiscal_year"
        data_tests: [unique, not_null]
      - name: rep_id
        description: "FK to dim_sales_rep"
        data_tests:
          - not_null
          - relationships:
              to: ref('dim_sales_rep')
              field: rep_id
      - name: fiscal_year
        description: "Fiscal year (same naming convention as dim_date.fiscal_year)"
        data_tests: [not_null]
```

---

## 6. MCP server — `mcp_server/dbt_mcp_server.py`

**Strict rule:** never write to `sys.stdout` in this module (no `print()`); FastMCP uses STDIO for JSON-RPC.

**Tools**

| Tool | Purpose |
|---|---|
| `list_data_lake_files(filter_keyword, include_schema)` | Lake files with the exact `data_lake/...` path to use in SQL, sizes, inferred columns. |
| `list_duckdb_tables(schema_name)` | Schemas, tables, views, columns in `warehouse.duckdb`. |
| `query_duckdb(sql, max_rows)` | One read-only statement across warehouse + lake; ≤ 500 rows; timeout. |
| `create_staging_model_from_lake(lake_file_path, table_name, source_name, materialized, overwrite)` | Registers the file in `sources.yml` (`meta.external_location`), writes `stg_<source>__<table>.sql` with snake_case aliases, runs `dbt parse`. CSV must be UTF-8. |
| `create_dim_date_model(start_date, end_date, fiscal_year_start_month, fiscal_year_naming, materialized, overwrite)` | `dim_date` with fiscal year/quarter/month; `fiscal_year_naming='end'` (default: Oct 2025 → FY2026, Thai government style) or `'start'`. |
| `generate_dbt_schema_tests(model_name, max_categorical_values, dry_run)` | Profiles a built model and merges proposed tests (see Section 0, items 7–8). |
| `get_dbt_metadata(model_name, force_refresh)` | Model metadata, lineage, properties file; auto-`dbt parse` when the manifest is stale. |
| `compile_dbt_model(select)` | Compiled SQL for any selector (`+fact_sales`, `tag:x`, `staging.*`). |
| `run_dbt_models(select, full_refresh)` / `run_dbt_tests(select)` | dbt run / test with structured per-node results; warnings reported separately. |

**Environment variables:** `DBT_PROJECT_DIR`, `DBT_PROFILES_DIR`, `DBT_BIN`, `DUCKDB_PATH`, `DATA_LAKE_DIR` (all default relative to the server file), `DBT_TIMEOUT_SEC` (300), `MCP_QUERY_TIMEOUT_SEC` (60), `MCP_DUCKDB_MEMORY_LIMIT` (`2GB`), `MCP_DUCKDB_THREADS` (4).

**`mcp_server/dbt_mcp_server.py`**

```python
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
from decimal import Decimal
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
def _json_default(value: Any) -> Any:
    """JSON fallback for values json can't encode (DECIMAL, DATE, TIMESTAMP, UUID, ...).

    DuckDB returns DECIMAL (e.g. SUM of a DECIMAL column) as Decimal. It becomes a JSON number
    when a float holds it exactly, so clients get numbers like they do for DOUBLE columns; a
    value that would lose digits as a float (e.g. DECIMAL(38,2) totals) stays an exact string.
    Everything else is rendered with str().
    """
    if isinstance(value, Decimal):
        if value.is_finite():
            as_float = float(value)
            if Decimal(repr(as_float)) == value:
                return as_float
        return str(value)
    return str(value)


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)


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
```

---

## 7. Optional enterprise stack (PostgreSQL + Cube Core + Metabase)

> **Status: tested live** on 2026-11-24 (Docker 29.9.3, macOS) from empty volumes, in both dev mode and production mode (`CUBEJS_DEV_MODE=false`). Image tags were the newest available that day. After any change to `cube.js`, the Cube model or the image tags, re-run `node docker/cube_rls_unit_check.js` and `scripts/check_enterprise_stack.py`.

**Data and access flow**

1. `dbt build` creates the marts in DuckDB.
2. `scripts/publish_marts_to_postgres.py` copies every `marts` table to Postgres `warehouse.marts` (needs network access once to install DuckDB's `postgres` extension; close other writers of `warehouse.duckdb` first).
3. Cube reads `warehouse.marts` and enforces RLS in `cube.js`. A single-node Cube Store container (`cubestore`, not published to the host) provides its cache and query queue.
4. Metabase connects to **Cube's SQL API** (Add database → PostgreSQL; host `cube`, port `15432`, database `db`, a user and password from `CUBE_SQL_USERS`, SSL off) so that dashboards inherit RLS. Add one Metabase database connection per security context you need (e.g. `admin`, `rep01`). Connecting Metabase directly to Postgres bypasses RLS; if you do that, limit access with Metabase's own permissions.

**Row-level security.** `role: admin` sees everything. Any other security context must carry `rep_id`; queries touching `Sales`, `SalesTargets` or `SalesReps` are filtered to that rep. REST/GraphQL take the context from the JWT signed with `CUBEJS_API_SECRET`; the SQL API maps each SQL user to a context via `CUBE_SQL_USERS`. A query without a valid context is refused (403 for a missing or invalid token when dev mode is off; `Access denied` from `cube.js` otherwise). In the Developer Playground, add the security context `{"role": "admin"}` (or a `rep_id`) before running queries.

`queryRewrite` adds the filter whenever a query touches one of the protected cubes, whether the member arrives as a string (`Sales.rep_id`) or, from the SQL API, as a member-expression object (`count(*)`, `upper(...)`, subqueries). Any member-expression object or subquery join that mentions a protected cube gets the filter, so an unfamiliar query shape is over-filtered or fails; it never skips the filter.

**Setup**

```bash
cp docker/.env.example docker/.env      # then change every secret
docker compose -f docker/docker-compose.yml up -d
uv run python scripts/publish_marts_to_postgres.py
node docker/cube_rls_unit_check.js                  # optional: offline RLS unit checks (needs Node)
uv run python scripts/check_enterprise_stack.py     # expect "ALL CHECKS PASSED"
```

Re-run `publish_marts_to_postgres.py` after every `dbt build` you want dashboards to see (it drops and recreates each table). The first `up` downloads about 4.3 GB of images.

`CUBEJS_DEV_MODE=true` (the `.env.example` default) is for the local demo: Developer Playground on `127.0.0.1:4000` and stack traces in API errors. Set it to `false` for anything shared; the bundled Cube Store works in both modes. Stop the stack with `docker compose -f docker/docker-compose.yml down` (add `-v` to delete the data volumes).

### docker/docker-compose.yml

**`docker/docker-compose.yml`**

```yaml
name: agentic-analytics

# Secrets come from docker/.env (copy .env.example). Ports bind to 127.0.0.1 only.
# Data path: dbt builds marts in DuckDB -> scripts/publish_marts_to_postgres.py -> Postgres
# -> Cube (semantic layer + RLS) -> Metabase (connect it to Cube's SQL API to inherit RLS).

services:
  postgres:
    image: postgres:16.16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER:?set POSTGRES_USER in docker/.env}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in docker/.env}
      POSTGRES_DB: warehouse
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U \"$${POSTGRES_USER}\" -d warehouse"]
      interval: 5s
      timeout: 5s
      retries: 10

  # Idempotent: creates Metabase's application database on every `up` if it is missing
  # (an /docker-entrypoint-initdb.d script would only run on a brand-new volume).
  db-init:
    image: postgres:16.16-alpine
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      PGHOST: postgres
      PGUSER: ${POSTGRES_USER}
      PGPASSWORD: ${POSTGRES_PASSWORD}
      PGDATABASE: warehouse
    entrypoint: ["sh", "-c"]
    command:
      - >-
        psql -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_database WHERE datname = 'metabase_app'" | grep -q 1
        || psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE metabase_app"
    restart: "no"

  cube:
    image: cubejs/cube:v1.8.32
    ports:
      - "127.0.0.1:4000:4000"     # REST / GraphQL / Playground (dev mode)
      - "127.0.0.1:15432:15432"   # Postgres-compatible SQL API
    environment:
      CUBEJS_DEV_MODE: ${CUBEJS_DEV_MODE:-true}
      CUBEJS_DB_TYPE: postgres
      CUBEJS_DB_HOST: postgres
      CUBEJS_DB_PORT: "5432"
      CUBEJS_DB_NAME: warehouse
      CUBEJS_DB_USER: ${POSTGRES_USER}
      CUBEJS_DB_PASS: ${POSTGRES_PASSWORD}
      CUBEJS_API_SECRET: ${CUBEJS_API_SECRET:?set CUBEJS_API_SECRET in docker/.env}
      CUBEJS_PG_SQL_PORT: "15432"  # the SQL API is only enabled when this is set
      CUBE_SQL_USERS: ${CUBE_SQL_USERS:?set CUBE_SQL_USERS in docker/.env}
      # Cache and query queue. Required when CUBEJS_DEV_MODE=false; also keeps dev mode from
      # starting an embedded Cube Store that writes into ./cube_schema/.cubestore.
      CUBEJS_CUBESTORE_HOST: cubestore
    volumes:
      - ./cube_schema:/cube/conf
    depends_on:
      postgres:
        condition: service_healthy
      cubestore:
        condition: service_started

  # Single-node Cube Store (cache + queue). Internal only: no ports published to the host.
  cubestore:
    image: cubejs/cubestore:v1.8.32
    environment:
      CUBESTORE_REMOTE_DIR: /cube/data
    volumes:
      - cube_store:/cube/data

  metabase:
    image: metabase/metabase:v0.65.6.3
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      MB_DB_TYPE: postgres
      MB_DB_DBNAME: metabase_app
      MB_DB_PORT: "5432"
      MB_DB_USER: ${POSTGRES_USER}
      MB_DB_PASS: ${POSTGRES_PASSWORD}
      MB_DB_HOST: postgres
    depends_on:
      db-init:
        condition: service_completed_successfully

volumes:
  pg_data:
  cube_store:
```

### docker/.env.example

**`docker/.env.example`**

```bash
# Copy to docker/.env and change every value. docker/.env is git-ignored.
POSTGRES_USER=analytics_admin
POSTGRES_PASSWORD=change-me-postgres

# Signs Cube REST/GraphQL JWTs. Use a long random string (e.g. `openssl rand -hex 32`).
CUBEJS_API_SECRET=change-me-long-random-secret

# true = local demo: Developer Playground on :4000 and error stack traces in API responses.
# false = production behaviour (every REST request needs a valid JWT). Both modes use the
# bundled cubestore service, so switching needs no other change.
CUBEJS_DEV_MODE=true

# Cube SQL API users (Postgres protocol on 127.0.0.1:15432) and their row-level security context.
# role=admin sees all rows; any other user sees only rows for its rep_id.
CUBE_SQL_USERS='{"admin":{"password":"change-me-admin","securityContext":{"role":"admin"}},"rep01":{"password":"change-me-rep01","securityContext":{"rep_id":"REP01"}}}'
```

### docker/cube_schema/model/sales.yml

**`docker/cube_schema/model/sales.yml`**

```yaml
cubes:
  - name: Sales
    sql_table: marts.fact_sales
    description: "Sales transactions (grain: one row per order line)"

    joins:
      - name: SalesReps
        sql: "{CUBE}.rep_id = {SalesReps}.rep_id"
        relationship: many_to_one

      - name: Courses
        sql: "{CUBE}.course_id = {Courses}.course_id"
        relationship: many_to_one

    measures:
      - name: total_revenue
        sql: amount
        type: sum
        title: "Total Revenue (THB)"
        description: "Order line revenue in Thai Baht (quantity x unit price; discounts are not modelled)"

      - name: order_count
        sql: order_id
        type: count_distinct
        title: "Order Count"

      - name: total_quantity
        sql: quantity
        type: sum
        title: "Seats / Units Sold"

    dimensions:
      - name: order_id
        sql: order_id
        type: string
        primary_key: true
        public: true

      - name: sale_date
        sql: sale_date
        type: time
        title: "Sale Date"

      - name: rep_id
        sql: rep_id
        type: string
        description: "Sales rep key (row-level security filter)"

      - name: region
        sql: region
        type: string

      - name: channel_type
        sql: channel_type
        type: string
        description: "Training delivery channel: Public (individual seats), In-house (corporate), or Hybrid"

  - name: SalesReps
    sql_table: marts.dim_sales_rep
    description: "Sales rep dimension"

    dimensions:
      - name: rep_id
        sql: rep_id
        type: string
        primary_key: true
        public: true

      - name: rep_name
        sql: rep_name
        type: string

  - name: Courses
    sql_table: marts.dim_course
    description: "Course dimension"

    dimensions:
      - name: course_id
        sql: course_id
        type: string
        primary_key: true

      - name: course_name
        sql: course_name
        type: string

  - name: SalesTargets
    sql_table: marts.fact_sales_target
    description: "Annual sales target per rep and fiscal year (THB)"

    joins:
      - name: SalesReps
        sql: "{CUBE}.rep_id = {SalesReps}.rep_id"
        relationship: many_to_one

    measures:
      - name: annual_target
        sql: annual_target
        type: sum
        title: "Annual Target (THB)"

    dimensions:
      - name: target_id
        sql: target_id
        type: string
        primary_key: true

      - name: rep_id
        sql: rep_id
        type: string

      - name: fiscal_year
        sql: fiscal_year
        type: number
```

### docker/cube_schema/cube.js

**`docker/cube_schema/cube.js`**

```javascript
// Row-level security (RLS) for the Cube semantic layer.
//
// Security context:
//   REST / GraphQL: claims of the JWT signed with CUBEJS_API_SECRET, e.g. {"rep_id": "REP01"}
//   SQL API (port 15432): looked up per SQL user in CUBE_SQL_USERS (see docker/.env.example)
// role "admin" sees everything; every other context must carry rep_id and is filtered to it.
// RLS only applies to queries that go THROUGH Cube. Direct Postgres or DuckDB access bypasses it.

const RLS_MEMBERS = {
  Sales: 'Sales.rep_id',
  SalesTargets: 'SalesTargets.rep_id',
  SalesReps: 'SalesReps.rep_id',
};

const RLS_CUBES = Object.keys(RLS_MEMBERS);

// Which cubes a query touches. A member is either a "Cube.member" string (REST/GraphQL and simple
// SQL API queries) or, when the SQL API turns SQL into member expressions (functions, count(*),
// subqueries, WHERE on an expression), an object with cubeName plus the cubes its SQL references.
// Objects and subqueryJoins are also scanned for RLS cube names, so an unfamiliar shape adds the
// filter (fail closed) instead of skipping it.
function cubesInQuery(query) {
  const cubes = new Set();
  const addRlsCubesNamedIn = (value) => {
    const text = JSON.stringify(value);
    for (const cube of RLS_CUBES) {
      if (new RegExp(`(^|[^A-Za-z0-9_])${cube}([^A-Za-z0-9_]|$)`).test(text)) cubes.add(cube);
    }
  };
  const addMember = (m) => {
    if (typeof m === 'string') {
      cubes.add(m.split('.')[0]);
    } else if (m && typeof m === 'object') {
      if (typeof m.cubeName === 'string') cubes.add(m.cubeName);
      addRlsCubesNamedIn(m);
    }
  };
  const addFilters = (filters) => {
    for (const f of filters || []) {
      addMember(f.member);
      addMember(f.dimension);
      addFilters(f.and);
      addFilters(f.or);
    }
  };
  for (const m of [...(query.measures || []), ...(query.dimensions || []), ...(query.segments || [])]) {
    addMember(m);
  }
  for (const td of query.timeDimensions || []) addMember(td.dimension);
  addFilters(query.filters);
  if (query.subqueryJoins && query.subqueryJoins.length) addRlsCubesNamedIn(query.subqueryJoins);
  return cubes;
}

module.exports = {
  queryRewrite: (query, { securityContext }) => {
    const ctx = securityContext || {};
    if (ctx.role === 'admin') {
      return query;
    }
    if (!ctx.rep_id) {
      throw new Error('Access denied: security context needs role=admin or rep_id');
    }
    const used = cubesInQuery(query);
    query.filters = query.filters || [];
    for (const [cube, member] of Object.entries(RLS_MEMBERS)) {
      if (used.has(cube)) {
        query.filters.push({ member, operator: 'equals', values: [String(ctx.rep_id)] });
      }
    }
    return query;
  },

  checkSqlAuth: async (req, username) => {
    let users;
    try {
      users = JSON.parse(process.env.CUBE_SQL_USERS || '{}');
    } catch (e) {
      throw new Error('CUBE_SQL_USERS is not valid JSON');
    }
    const user = users[username];
    if (!user || !user.password) {
      throw new Error('Incorrect user name or password');
    }
    return { password: user.password, securityContext: user.securityContext || {} };
  },
};
```

### scripts/publish_marts_to_postgres.py

**`scripts/publish_marts_to_postgres.py`**

```python
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
```

### scripts/check_enterprise_stack.py

**`scripts/check_enterprise_stack.py`**

```python
"""Smoke-test the running enterprise stack: published marts, Cube REST RLS and Cube SQL API RLS.

Usage (after `docker compose up -d` and `scripts/publish_marts_to_postgres.py`):
    uv run python scripts/check_enterprise_stack.py

Reads docker/.env. Needs only the Python standard library and the `docker` CLI: SQL queries run
with psql inside the postgres container, so nothing else has to be installed on the host.
Checks:
  * Postgres has the marts tables.
  * REST: an admin JWT sees several reps; a rep JWT sees only its own rep; no token is refused.
  * SQL API (the path Metabase uses): every user in CUBE_SQL_USERS gets the rows its security
    context allows, including queries Cube turns into member expressions (count(*), functions).
Exit code 0 = all checks passed, 1 = a check failed.
"""

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker" / "docker-compose.yml")]
CUBE_REST = os.environ.get("CUBE_REST_URL", "http://127.0.0.1:4000/cubejs-api/v1")
FAILURES: list[str] = []


def check(ok: bool, label: str, detail: object = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail != "" else ""))
    if not ok:
        FAILURES.append(label)


def load_env() -> dict[str, str]:
    env_file = ROOT / "docker" / ".env"
    if not env_file.exists():
        raise SystemExit("docker/.env not found (copy docker/.env.example and set the secrets).")
    env = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'\"")
    return env


def psql(user: str, password: str, sql: str, host: str = "cube", port: str = "15432", db: str = "db") -> tuple[bool, list[list[str]]]:
    """Run one query with psql inside the postgres container. Returns (ok, rows or [[error]])."""
    proc = subprocess.run(
        COMPOSE + ["exec", "-T", "-e", "PGPASSWORD", "postgres",
                   "psql", "-h", host, "-p", port, "-U", user, "-d", db,
                   "-v", "ON_ERROR_STOP=1", "-tA", "-F", "\t", "-c", sql],
        env={**os.environ, "PGPASSWORD": password}, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        return False, [[(proc.stderr or proc.stdout).strip().splitlines()[0] if (proc.stderr or proc.stdout).strip() else "psql failed"]]
    return True, [line.split("\t") for line in proc.stdout.splitlines() if line]


def jwt(secret: str, claims: dict) -> str:
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({**claims, "exp": int(time.time()) + 300}).encode())
    signature = b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def rest_load(query: dict, token: str | None) -> tuple[bool, object]:
    url = f"{CUBE_REST}/load?query={urllib.parse.quote(json.dumps(query))}"
    for _ in range(60):
        request = urllib.request.Request(url, headers={"Authorization": token} if token else {})
        try:
            body = json.load(urllib.request.urlopen(request, timeout=60))
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return False, f"Cube not reachable at {CUBE_REST} ({exc.reason})"
        if body.get("error") == "Continue wait":
            time.sleep(1)
            continue
        return True, body.get("data", [])
    return False, "timed out waiting for Cube"


def main() -> int:
    env = load_env()
    users = json.loads(env.get("CUBE_SQL_USERS") or "{}")
    # admins first, so their row count is known before the filtered users are compared with it
    rep_contexts = {u: v.get("securityContext", {}) for u, v in
                    sorted(users.items(), key=lambda item: item[1].get("securityContext", {}).get("role") != "admin")}
    sample_rep = next((c["rep_id"] for c in rep_contexts.values() if c.get("rep_id")), "REP01")

    print("Postgres (published marts)")
    ok, rows = psql(env["POSTGRES_USER"], env["POSTGRES_PASSWORD"],
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'marts' ORDER BY 1",
                    host="localhost", port="5432", db="warehouse")
    tables = [r[0] for r in rows] if ok else []
    needed = {"fact_sales", "dim_sales_rep", "dim_course", "fact_sales_target"}
    check(ok and needed <= set(tables), "marts tables published", tables if ok else rows[0][0])

    print("Cube REST API (JWT security context)")
    by_rep = {"measures": ["Sales.total_revenue"], "dimensions": ["Sales.rep_id"]}
    targets = {"measures": ["SalesTargets.annual_target"], "dimensions": ["SalesTargets.rep_id"]}
    secret = env["CUBEJS_API_SECRET"]
    ok, data = rest_load(by_rep, jwt(secret, {"role": "admin"}))
    admin_reps = sorted({row["Sales.rep_id"] for row in data}) if ok else []
    check(ok and len(admin_reps) > 1, "admin sees all reps", admin_reps if ok else data)
    for query, member in [(by_rep, "Sales.rep_id"), (targets, "SalesTargets.rep_id")]:
        ok, data = rest_load(query, jwt(secret, {"rep_id": sample_rep}))
        reps = sorted({row[member] for row in data}) if ok else data
        check(ok and reps == [sample_rep], f"rep_id={sample_rep} sees only own rows ({member.split('.')[0]})", reps)
    ok, data = rest_load(by_rep, None)
    check(not ok, "request without token is refused", data if not ok else "returned data")
    ok, data = rest_load(by_rep, jwt(secret, {}))
    check(not ok, "token without role/rep_id is refused", data if not ok else "returned data")

    print("Cube SQL API (CUBE_SQL_USERS; Metabase connects here)")
    queries = {
        "revenue by rep": "SELECT rep_id, MEASURE(total_revenue) FROM Sales GROUP BY 1 ORDER BY 1",
        "function on dimension": "SELECT upper(rep_id), MEASURE(total_revenue) FROM Sales GROUP BY 1 ORDER BY 1",
        "targets by rep": "SELECT rep_id FROM SalesTargets GROUP BY 1 ORDER BY 1",
    }
    admin_count = None
    for user, ctx in rep_contexts.items():
        password = users[user].get("password", "")
        for label, sql in queries.items():
            ok, rows = psql(user, password, sql)
            reps = sorted({r[0].upper() for r in rows}) if ok else rows[0][0]
            if ctx.get("role") == "admin":
                check(ok and len(reps) > 1, f"{user}: {label} shows all reps", reps)
            else:
                check(ok and reps == [str(ctx.get("rep_id", "")).upper()], f"{user}: {label} filtered", reps)
        ok, rows = psql(user, password, "SELECT count(*) FROM Sales")
        count = int(rows[0][0]) if ok else rows[0][0]
        if ctx.get("role") == "admin":
            admin_count = count
            check(ok, f"{user}: count(*) works", count)
        else:
            check(ok and isinstance(admin_count, int) and count < admin_count, f"{user}: count(*) filtered", f"{count} of {admin_count}")
    ok, rows = psql(next(iter(users), "nobody"), "wrong-password", "SELECT 1")
    check(not ok, "wrong SQL API password is refused", rows[0][0] if not ok else "accepted")

    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} CHECK(S) FAILED'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
```

### docker/cube_rls_unit_check.js

**`docker/cube_rls_unit_check.js`**

```javascript
// Offline unit checks of cube.js RLS logic (no Cube needed): node docker/cube_rls_unit_check.js
const path = process.argv[2] || require('path').join(__dirname, 'cube_schema', 'cube.js');
process.env.CUBE_SQL_USERS = JSON.stringify({ admin: { password: 'a', securityContext: { role: 'admin' } }, rep01: { password: 'r', securityContext: { rep_id: 'REP01' } } });
const cfg = require(path);
const assert = require('assert');
const q = () => ({ measures: ['Sales.total_revenue'], dimensions: ['Courses.course_name'], filters: [] });
assert.deepStrictEqual(cfg.queryRewrite(q(), { securityContext: { role: 'admin' } }).filters, []);
const r = cfg.queryRewrite(q(), { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(r.filters, [{ member: 'Sales.rep_id', operator: 'equals', values: ['REP01'] }]);
const t = cfg.queryRewrite({ measures: ['SalesTargets.annual_target'], dimensions: ['SalesReps.rep_name'] }, { securityContext: { rep_id: 'REP02' } });
assert.strictEqual(t.filters.length, 2);
const c = cfg.queryRewrite({ dimensions: ['Courses.course_name'] }, { securityContext: { rep_id: 'REP02' } });
assert.deepStrictEqual(c.filters, []);
// SQL API member expressions (shapes captured from Cube v1.8.32)
const expr = (cube, name, sql, params) => ({ cubeName: cube, name, expressionName: name, expression: [...params, `return \`${sql}\``],
  definition: JSON.stringify({ cubeName: cube, alias: name, expr: { type: 'SqlFunction', cubeParams: params, sql } }) });
const e1 = cfg.queryRewrite({ measures: [expr('Sales', 'count_uint8_1__', 'COUNT(*)', ['Sales'])], dimensions: [], segments: [], filters: [], timeDimensions: [], subqueryJoins: [] }, { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(e1.filters, [{ member: 'Sales.rep_id', operator: 'equals', values: ['REP01'] }]);
const e2 = cfg.queryRewrite({ measures: [], dimensions: [expr('Courses', 'x', '${Courses.course_name} || ${SalesTargets.rep_id}', ['Courses', 'SalesTargets'])], filters: [] }, { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(e2.filters.map((f) => f.member), ['SalesTargets.rep_id']);
const e3 = cfg.queryRewrite({ dimensions: ['Courses.course_name'], segments: [expr('Courses', 'seg', '(UPPER(${Courses.course_name}) = $0$)', ['Courses'])], filters: [] }, { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(e3.filters, []);
const e4 = cfg.queryRewrite({ dimensions: ['Courses.course_name'], filters: [], subqueryJoins: [{ sql: 'SELECT "SalesReps".rep_id FROM ...', on: '', joinType: 'LEFT', alias: 'sq' }] }, { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(e4.filters.map((f) => f.member), ['SalesReps.rep_id']);
assert.throws(() => cfg.queryRewrite(q(), { securityContext: {} }), /Access denied/);
(async () => {
  assert.deepStrictEqual(await cfg.checkSqlAuth({}, 'rep01'), { password: 'r', securityContext: { rep_id: 'REP01' } });
  await assert.rejects(cfg.checkSqlAuth({}, 'nobody'), /Incorrect/);
  console.log('cube.js RLS unit checks: OK');
})();
```

---

## 8. MCP client configuration

`.mcp.json` must contain absolute paths for the current machine, so it is **generated** (and git-ignored) instead of committed:

```bash
uv run python scripts/write_mcp_config.py
```

The script writes `.mcp.json` for Claude Code (project scope) and prints the same block for Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows). If a long `dbt run` exceeds your MCP client's tool-call timeout, raise the client timeout or lower `DBT_TIMEOUT_SEC` and run large builds from the terminal.

### scripts/write_mcp_config.py

**`scripts/write_mcp_config.py`**

```python
"""Write .mcp.json (Claude Code, project scope) for THIS checkout and print the
Claude Desktop snippet. .mcp.json contains machine-specific absolute paths, so it is
generated per machine and git-ignored; .mcp.json.example shows the shape.

Usage:  uv run python scripts/write_mcp_config.py
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_BIN = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
EXE = ".exe" if os.name == "nt" else ""


def main() -> None:
    python_bin = VENV_BIN / f"python{EXE}"
    dbt_bin = VENV_BIN / f"dbt{EXE}"
    if not python_bin.exists():
        raise SystemExit(f"{python_bin} not found. Run 'uv sync' first.")

    server = {
        "command": str(python_bin),
        "args": [str(ROOT / "mcp_server" / "dbt_mcp_server.py")],
        "env": {
            "DBT_PROJECT_DIR": str(ROOT),
            "DBT_PROFILES_DIR": str(ROOT),
            "DBT_BIN": str(dbt_bin),
            "DUCKDB_PATH": str(ROOT / "warehouse.duckdb"),
            "DATA_LAKE_DIR": str(ROOT / "data_lake"),
            "MCP_QUERY_TIMEOUT_SEC": "60",
            "DBT_TIMEOUT_SEC": "300",
        },
    }
    config = {"mcpServers": {"dbt-duckdb": server}}
    target = ROOT / ".mcp.json"
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target}")
    print(
        "\nClaude Desktop: merge this into claude_desktop_config.json\n"
        "  macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json\n"
        "  Windows: %APPDATA%\\Claude\\claude_desktop_config.json\n"
    )
    print(json.dumps(config, indent=2))


if __name__ == "__main__":
    main()
```

### .mcp.json.example

**`.mcp.json.example`**

```json
{
  "mcpServers": {
    "dbt-duckdb": {
      "command": "<ABS_PROJECT_DIR>/.venv/bin/python",
      "args": ["<ABS_PROJECT_DIR>/mcp_server/dbt_mcp_server.py"],
      "env": {
        "DBT_PROJECT_DIR": "<ABS_PROJECT_DIR>",
        "DBT_PROFILES_DIR": "<ABS_PROJECT_DIR>",
        "DBT_BIN": "<ABS_PROJECT_DIR>/.venv/bin/dbt",
        "DUCKDB_PATH": "<ABS_PROJECT_DIR>/warehouse.duckdb",
        "DATA_LAKE_DIR": "<ABS_PROJECT_DIR>/data_lake",
        "MCP_QUERY_TIMEOUT_SEC": "60",
        "DBT_TIMEOUT_SEC": "300"
      }
    }
  }
}
```

---

## 9. Automated verification suite — `tests/test_mcp_server.py`

Claude Code MUST create and run this suite. It copies the project into a temp directory, so it never touches the real models or warehouse. It covers the end-to-end pipeline, fiscal-year boundaries, every sandbox bypass found in the v1.0 review, statement classification, query timeout, stale run results, YAML merge, cross-process lock reporting, and the MCP protocol over STDIO with the server launched from `/`.

**`tests/test_mcp_server.py`**

```python
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


def test_decimal_results_are_json_numbers(m, built):
    q = _j(m.query_duckdb(
        "SELECT SUM(amount) AS total, CAST(1.10 AS DECIMAL(10, 2)) AS small, "
        "CAST('12345678901234567890.12' AS DECIMAL(38, 2)) AS big, DATE '2026-01-31' AS day "
        "FROM marts.fact_sales"
    ))
    assert q["success"], q
    row = q["rows"][0]
    assert isinstance(row["total"], float) and row["total"] > 0, row
    assert row["small"] == 1.1
    assert row["big"] == "12345678901234567890.12"  # a float would lose digits: stays exact text
    assert row["day"] == "2026-01-31"


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
```

---

## 10. Claude Code execution checklist

Run these steps in order from the project root.

1. **Scaffold files** from Sections 4–9 exactly as written. Create empty `models/staging/`, `models/marts/` and `data_tests/` directories (with `.gitkeep`).
2. **Install** (uv picks Python 3.12 from `.python-version`; 3.11 also works):
   ```bash
   uv sync
   ```
3. **Generate data & check dbt:**
   ```bash
   uv run python generate_mock_lake.py
   uv run dbt debug --profiles-dir .
   ```
4. **Run the test suite** (expect 47 passed):
   ```bash
   uv run pytest -v
   ```
5. **Register the MCP server**, then restart Claude Code / Claude Desktop:
   ```bash
   uv run python scripts/write_mcp_config.py
   ```
6. **Live prompt test** in Claude Code:
   > Run list_data_lake_files. Scaffold staging models for both lake files with create_staging_model_from_lake. Generate dim_date for 2025-01-01 to 2027-12-31 with an October fiscal start via create_dim_date_model. Copy every file from starter_marts/ into models/marts/. Execute run_dbt_models, profile stg_data_lake__raw_sales_2026 with generate_dbt_schema_tests, confirm zero failures with run_dbt_tests, then use query_duckdb to summarise FY2026 actual sales vs. target per sales rep from marts.fact_sales, marts.dim_date, marts.dim_sales_rep and marts.fact_sales_target.

   Expected: 4 reps for FY2026; `run_dbt_tests` succeeds with 0 failures.
7. *(Optional)* **Enterprise stack:** follow Section 7 (needs Docker). Expected: `publish_marts_to_postgres.py` lists 5 tables and `check_enterprise_stack.py` prints `ALL CHECKS PASSED`.
8. *(Optional)* **Sales dashboard:** after step 6, add the Section 11 files and the `dashboard` dependency group, then:
   ```bash
   uv run --group dashboard pytest tests/test_dashboard.py -v      # expect 8 passed
   uv run --group dashboard streamlit run dashboard/app.py          # http://localhost:8501
   ```
   Expected for FY2026: revenue ฿768,400, 15 orders, 57 seats, target ฿820,000, 93.7% of target. To host it, follow Section 11.3.

---

## 11. Sales dashboard & hosting (optional add-on)

A small web page for one trusted viewer, built on the marts from step 6 of Section 10. Unlike Sections 4–9, these files are not part of `reference/agentic_analytics_demo/`; they are copied from the implemented project `agentic_analytics_demo/`, which has the same layout.

### 11.1 Rules (keep these if you change the dashboard)

| Rule | Why |
|---|---|
| Open `warehouse.duckdb` with `read_only=True` and `enable_external_access=false`, in a short-lived connection per refresh (`dashboard/data.py`, `_query`). | DuckDB allows one writer *or* many readers. A long-lived connection would block `run_dbt_models`; a busy file is reported on the page instead of crashing it. |
| No row-level security: everyone who can open the page sees every row. Locally bind to `127.0.0.1` (`.streamlit/config.toml`); hosted, put a login in front (Cloudflare Access). | Per-user dashboards belong in Metabase via Cube (Section 7). |
| Targets exist per rep and fiscal year, so target figures ignore the region, channel and course filters, and the page says so. | Otherwise % of target would compare filtered actuals with an unfiltered target. |
| Fiscal year runs October–September, named by its end year (`dim_date`); the month chart always shows all 12 fiscal months, zero-filled. | Matches the `fiscal_year_naming = end` setting. |
| Charts use one colour pair that passes contrast and colour-blind checks in both light and dark themes (`#3987e5` actual, `#d95926` target), with a legend, tooltips and a table view. | Streamlit's theme detection lags, so per-theme colours flicker. |

### 11.2 Files

Add the optional dependency group to `pyproject.toml` and refresh the lock file (`uv lock`):

```toml
[dependency-groups]
dashboard = [
    # Hosted image installs only this group (uv sync --only-group dashboard), so list duckdb too.
    "duckdb>=1.3.0,<3.0.0",
    "streamlit>=1.66,<2",
]
```

**`.streamlit/config.toml`**

```toml
# Streamlit settings for dashboard/app.py (read when you run streamlit from the project root).
[server]
address = "127.0.0.1"   # local only: the dashboard has no row-level security
port = 8501
headless = true

[browser]
gatherUsageStats = false
serverAddress = "localhost"

[client]
toolbarMode = "viewer"   # hide Streamlit's Deploy button and developer menu items
```

**`dashboard/data.py`**

```python
"""Read-only data access and aggregations for the local sales dashboard.

Reads the dbt `marts` schema of warehouse.duckdb. Every query opens its own read-only
connection and closes it straight away, so the dashboard never holds DuckDB's file lock
while dbt (or the MCP server) needs to write. No row-level security: every row in the
warehouse is visible, which is why the app only listens on 127.0.0.1.
"""

from __future__ import annotations

import os
from contextlib import closing
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent


class WarehouseUnavailable(RuntimeError):
    """The warehouse file is missing, locked by a writer, or has no marts yet."""


def warehouse_path() -> Path:
    raw = os.environ.get("DASHBOARD_DUCKDB_PATH") or os.environ.get("DUCKDB_PATH") or PROJECT_DIR / "warehouse.duckdb"
    return Path(raw).resolve()


def _query(sql: str, params: list | None = None) -> pd.DataFrame:
    path = warehouse_path()
    if not path.exists():
        raise WarehouseUnavailable(f"{path} not found. Build the marts first (run_dbt_models or `dbt build`).")
    try:
        # read_only + closed after each query; external access off: the app reads only this file
        with closing(duckdb.connect(str(path), read_only=True, config={"enable_external_access": False})) as con:
            return con.execute(sql, params or []).df()
    except duckdb.IOException as exc:
        if "lock" in str(exc).lower():
            raise WarehouseUnavailable(
                "warehouse.duckdb is locked by a writer (a dbt run, the DuckDB CLI or DBeaver). "
                "Refresh when it has finished."
            ) from exc
        raise
    except duckdb.CatalogException as exc:
        raise WarehouseUnavailable(f"The marts are missing or incomplete: {exc}") from exc


def load_sales_lines() -> pd.DataFrame:
    """One row per order line, with fiscal calendar, rep and course names."""
    df = _query(
        """
        SELECT
            f.order_id,
            f.sale_date,
            CAST(date_trunc('month', f.sale_date) AS DATE) AS month_start,
            d.fiscal_year,
            d.fiscal_year_label,
            d.fiscal_month_number,
            f.rep_id,
            r.rep_name,
            c.course_name,
            f.region,
            f.channel_type,
            f.quantity,
            CAST(f.unit_price AS DOUBLE) AS unit_price,
            CAST(f.amount AS DOUBLE) AS amount
        FROM marts.fact_sales f
        JOIN marts.dim_date d ON f.date_key = d.date_key
        JOIN marts.dim_sales_rep r ON f.rep_id = r.rep_id
        JOIN marts.dim_course c ON f.course_id = c.course_id
        ORDER BY f.sale_date, f.order_id
        """
    )
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    df["month_start"] = pd.to_datetime(df["month_start"])
    return df


def load_targets() -> pd.DataFrame:
    return _query(
        """
        SELECT t.fiscal_year, t.rep_id, r.rep_name, CAST(t.annual_target AS DOUBLE) AS annual_target
        FROM marts.fact_sales_target t
        JOIN marts.dim_sales_rep r ON t.rep_id = r.rep_id
        ORDER BY t.fiscal_year, t.rep_id
        """
    )


def load_fiscal_years() -> pd.DataFrame:
    """Fiscal years in dim_date with their first/last day and month starts (Oct..Sep)."""
    df = _query(
        """
        SELECT fiscal_year, any_value(fiscal_year_label) AS fiscal_year_label,
               min(date_day) AS first_day, max(date_day) AS last_day
        FROM marts.dim_date GROUP BY fiscal_year ORDER BY fiscal_year
        """
    )
    df["first_day"] = pd.to_datetime(df["first_day"])
    df["last_day"] = pd.to_datetime(df["last_day"])
    return df


def default_fiscal_year(fiscal_years: pd.DataFrame, lines: pd.DataFrame, today: date) -> int:
    """The latest fiscal year that has ended and has sales; else the latest year with sales."""
    with_sales = set(lines["fiscal_year"].unique())
    candidates = fiscal_years[fiscal_years["fiscal_year"].isin(with_sales)]
    if candidates.empty:
        return int(fiscal_years["fiscal_year"].max())
    finished = candidates[candidates["last_day"].dt.date < today]
    pool = finished if not finished.empty else candidates
    return int(pool["fiscal_year"].max())


def filter_lines(lines: pd.DataFrame, fiscal_year: int, reps=None, regions=None, channels=None, courses=None) -> pd.DataFrame:
    out = lines[lines["fiscal_year"] == fiscal_year]
    for column, chosen in (("rep_name", reps), ("region", regions), ("channel_type", channels), ("course_name", courses)):
        if chosen:
            out = out[out[column].isin(chosen)]
    return out


def rep_vs_target(lines: pd.DataFrame, targets: pd.DataFrame, fiscal_year: int, reps=None) -> pd.DataFrame:
    """Actual vs. target per rep for one fiscal year. Targets exist per rep and year only,
    so this ignores the region/channel/course filters on purpose."""
    year_targets = targets[targets["fiscal_year"] == fiscal_year][["rep_id", "rep_name", "annual_target"]]
    actual = (
        lines[lines["fiscal_year"] == fiscal_year]
        .groupby(["rep_id", "rep_name"], as_index=False)["amount"].sum()
        .rename(columns={"amount": "actual"})
    )
    out = year_targets.merge(actual, on=["rep_id", "rep_name"], how="outer")
    out["actual"] = out["actual"].fillna(0.0)
    out = out.rename(columns={"annual_target": "target"})
    out["pct_of_target"] = (out["actual"] / out["target"] * 100).where(out["target"] > 0).round(1)
    if reps:
        out = out[out["rep_name"].isin(reps)]
    return out.sort_values("rep_id").reset_index(drop=True)


def monthly_revenue(lines: pd.DataFrame, fiscal_years: pd.DataFrame, fiscal_year: int) -> pd.DataFrame:
    """Revenue per month of the fiscal year, with empty months shown as 0."""
    fy = fiscal_years[fiscal_years["fiscal_year"] == fiscal_year].iloc[0]
    months = pd.date_range(fy["first_day"], fy["last_day"], freq="MS")
    by_month = lines.groupby("month_start")["amount"].sum()
    out = pd.DataFrame({"month_start": months})
    out["revenue"] = out["month_start"].map(by_month).fillna(0.0)
    out["orders"] = out["month_start"].map(lines.groupby("month_start")["order_id"].nunique()).fillna(0).astype(int)
    return out


def revenue_by(lines: pd.DataFrame, column: str) -> pd.DataFrame:
    return (
        lines.groupby(column, as_index=False)
        .agg(revenue=("amount", "sum"), orders=("order_id", "nunique"), seats=("quantity", "sum"))
        .sort_values("revenue", ascending=False)
        .reset_index(drop=True)
    )


def kpis(lines: pd.DataFrame, rep_targets: pd.DataFrame) -> dict:
    target = float(rep_targets["target"].fillna(0).sum())
    actual_for_target = float(rep_targets["actual"].sum())
    return {
        "revenue": float(lines["amount"].sum()),
        "orders": int(lines["order_id"].nunique()),
        "seats": int(lines["quantity"].sum()),
        "target": target,
        "pct_of_target": round(actual_for_target / target * 100, 1) if target else None,
    }
```

**`dashboard/app.py`**

```python
"""Sales dashboard (hosted at https://agentic-dashboard.praponb.com behind Cloudflare Access).

Run locally from the project root:
    uv sync --group dashboard
    uv run --group dashboard streamlit run dashboard/app.py

Locally it listens on http://127.0.0.1:8501 only (see .streamlit/config.toml); the hosted copy
(Dockerfile.dashboard, deploy/) is reachable only through a Cloudflare Access login. It shows
every row in the warehouse, with no row-level security. For per-user dashboards use Metabase
via Cube.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402  (local module next to this file)

st.set_page_config(page_title="Agentic Analytics Dashboard", page_icon="📊", layout="wide")

# Categorical slots 1-2 (blue, orange), dark-surface steps. This pair passes the palette
# checks (lightness, chroma, colour-blind separation, 3:1 contrast) on BOTH the light and the
# dark theme, so it needs no theme detection. Single-series charts use blue alone.
BLUE, ORANGE = "#3987e5", "#d95926"
THB = ",.0f"


@st.cache_data(ttl=60, show_spinner=False)
def load(mtime: float):
    """Cached for 60 s and per file version (mtime), so a new dbt build shows up on refresh."""
    return data.load_sales_lines(), data.load_targets(), data.load_fiscal_years()


def fmt_thb(value: float) -> str:
    return f"฿{value:,.0f}"


st.title("Agentic Analytics Dashboard")
st.caption("agentic-dashboard.praponb.com · sales from the dbt marts in warehouse.duckdb")

path = data.warehouse_path()
try:
    lines, targets, fiscal_years = load(path.stat().st_mtime if path.exists() else 0.0)
except data.WarehouseUnavailable as exc:
    st.error(str(exc))
    st.stop()

if lines.empty:
    st.warning("marts.fact_sales has no rows yet.")
    st.stop()

# ---- Filters: one row above the charts -------------------------------------------------
years = sorted(lines["fiscal_year"].unique().tolist())
labels = dict(zip(fiscal_years["fiscal_year"], fiscal_years["fiscal_year_label"]))
today = date.today()
default_year = data.default_fiscal_year(fiscal_years, lines, today)


def year_label(fy: int) -> str:
    row = fiscal_years[fiscal_years["fiscal_year"] == fy]
    in_progress = not row.empty and row.iloc[0]["first_day"].date() <= today <= row.iloc[0]["last_day"].date()
    return labels.get(fy, f"FY{fy}") + (" (in progress)" if in_progress else "")


f1, f2, f3, f4, f5 = st.columns([1.1, 1.6, 1.6, 1.3, 1.8])
fiscal_year = f1.selectbox("Fiscal year", years, index=years.index(default_year), format_func=year_label, key="fy")
reps = f2.multiselect("Sales rep", sorted(lines["rep_name"].unique()), placeholder="All reps", key="reps")
regions = f3.multiselect("Region", sorted(lines["region"].unique()), placeholder="All regions", key="regions")
channels = f4.multiselect("Channel", sorted(lines["channel_type"].unique()), placeholder="All channels", key="channels")
courses = f5.multiselect("Course", sorted(lines["course_name"].unique()), placeholder="All courses", key="courses")

selected = data.filter_lines(lines, fiscal_year, reps, regions, channels, courses)
rep_targets = data.rep_vs_target(lines, targets, fiscal_year, reps)
k = data.kpis(selected, rep_targets)

# ---- KPI tiles -------------------------------------------------------------------------
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Revenue", fmt_thb(k["revenue"]))
m2.metric("Orders", f"{k['orders']:,}")
m3.metric("Seats sold", f"{k['seats']:,}")
m4.metric("Target", fmt_thb(k["target"]) if k["target"] else "No target")
m5.metric("% of target", f"{k['pct_of_target']:.1f}%" if k["pct_of_target"] is not None else "–")
if regions or channels or courses:
    st.caption("Target and % of target are set per rep and fiscal year, so they ignore the region, channel and course filters.")

# ---- Actual vs target | monthly trend -----------------------------------------------------
left, right = st.columns(2)

with left:
    st.subheader("Actual vs. target by rep")
    if rep_targets.empty:
        st.info("No reps or targets for this selection.")
    else:
        long = rep_targets.melt(
            id_vars=["rep_name", "pct_of_target"], value_vars=["actual", "target"], var_name="measure", value_name="thb"
        )
        long["measure"] = long["measure"].map({"actual": "Actual", "target": "Target"})
        bars = (
            alt.Chart(long)
            .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
            .encode(
                x=alt.X("rep_name:N", title=None, axis=alt.Axis(labelAngle=0)),
                xOffset=alt.XOffset("measure:N", scale=alt.Scale(paddingInner=0.08)),
                y=alt.Y("thb:Q", title="THB", axis=alt.Axis(format="~s")),
                color=alt.Color(
                    "measure:N",
                    scale=alt.Scale(domain=["Actual", "Target"], range=[BLUE, ORANGE]),
                    legend=alt.Legend(title=None, orient="top"),
                ),
                tooltip=[
                    alt.Tooltip("thb:Q", title="THB", format=THB),
                    alt.Tooltip("measure:N", title="Measure"),
                    alt.Tooltip("rep_name:N", title="Rep"),
                    alt.Tooltip("pct_of_target:Q", title="% of target", format=".1f"),
                ],
            )
            .properties(height=320)
        )
        st.altair_chart(bars, width="stretch")

with right:
    st.subheader("Revenue by month")
    monthly = data.monthly_revenue(selected, fiscal_years, fiscal_year)
    hover = alt.selection_point(fields=["month_start"], nearest=True, on="pointerover", empty=False, clear="pointerout")
    base = alt.Chart(monthly).encode(x=alt.X("month_start:T", title=None, axis=alt.Axis(format="%b %Y", labelAngle=0, tickCount="month")))
    line = base.mark_line(strokeWidth=2, color=BLUE).encode(y=alt.Y("revenue:Q", title="THB", axis=alt.Axis(format="~s")))
    points = base.mark_point(filled=True, size=70, color=BLUE).encode(
        y="revenue:Q", opacity=alt.condition(hover, alt.value(1), alt.value(0))
    )
    rule = (
        base.mark_rule(strokeWidth=1, color="gray")
        .encode(
            opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
            tooltip=[
                alt.Tooltip("revenue:Q", title="Revenue (THB)", format=THB),
                alt.Tooltip("orders:Q", title="Orders"),
                alt.Tooltip("month_start:T", title="Month", format="%B %Y"),
            ],
        )
        .add_params(hover)
    )
    st.altair_chart((line + points + rule).properties(height=320), width="stretch")


# ---- Breakdowns --------------------------------------------------------------------------
def breakdown(df: pd.DataFrame, column: str, title: str) -> alt.Chart:
    return (
        alt.Chart(df)
        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, color=BLUE)
        .encode(
            y=alt.Y(f"{column}:N", sort="-x", title=None),
            x=alt.X("revenue:Q", title="THB", axis=alt.Axis(format="~s")),
            tooltip=[
                alt.Tooltip("revenue:Q", title="Revenue (THB)", format=THB),
                alt.Tooltip(f"{column}:N", title=title),
                alt.Tooltip("orders:Q", title="Orders"),
                alt.Tooltip("seats:Q", title="Seats"),
            ],
        )
        .properties(height=alt.Step(28))  # fixed px per bar: every label fits, however few bars
    )


c1, c2, c3 = st.columns([1.4, 1, 1])
for col, column, title in ((c1, "course_name", "Course"), (c2, "region", "Region"), (c3, "channel_type", "Channel")):
    with col:
        st.subheader(f"Revenue by {title.lower()}")
        table = data.revenue_by(selected, column)
        if table.empty:
            st.info("No sales for this selection.")
        else:
            st.altair_chart(breakdown(table, column, title), width="stretch")

# ---- Table view ------------------------------------------------------------------------------
st.subheader(f"Order lines ({len(selected):,})")
detail = selected[["sale_date", "order_id", "rep_name", "course_name", "region", "channel_type", "quantity", "unit_price", "amount"]]
st.dataframe(
    detail,
    hide_index=True,
    width="stretch",
    column_config={
        "sale_date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
        "order_id": "Order",
        "rep_name": "Rep",
        "course_name": "Course",
        "region": "Region",
        "channel_type": "Channel",
        "quantity": st.column_config.NumberColumn("Seats"),
        "unit_price": st.column_config.NumberColumn("Unit price (THB)", format="%,.0f"),
        "amount": st.column_config.NumberColumn("Amount (THB)", format="%,.0f"),
    },
)
st.download_button(
    "Download CSV",
    detail.to_csv(index=False).encode("utf-8"),
    file_name=f"sales_{labels.get(fiscal_year, fiscal_year)}.csv",
    mime="text/csv",
)

updated = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
st.caption(
    f"Data: {path.name}, last built {updated:%Y-%m-%d %H:%M %Z}. "
    "No row-level security: this page shows every row, so keep it on 127.0.0.1 or behind a login."
)
```

**`tests/test_dashboard.py`**

```python
"""Tests for the local Streamlit dashboard (dashboard/). Skipped unless the dashboard
dependency group is installed:  uv sync --group dashboard && uv run pytest tests/test_dashboard.py

Uses a small synthetic warehouse (same marts columns as the dbt project), so it does not
need a dbt build.
"""

import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import pytest

pytest.importorskip("pandas")
pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
APP = PROJECT / "dashboard" / "app.py"
sys.path.insert(0, str(PROJECT / "dashboard"))
import data  # noqa: E402

# FY2026 = Oct 2025 - Sep 2026 (fiscal year named by the year it ends in)
SALES = [
    # order_id, sale_date, rep_id, course, region, channel, qty, unit_price
    ("O1", "2025-10-05", "R1", "Course A", "Bangkok", "Public", 2, 1000.0),
    ("O2", "2025-12-10", "R1", "Course B", "North", "In-house", 1, 5000.0),
    ("O3", "2026-03-15", "R2", "Course A", "Bangkok", "Hybrid", 3, 1000.0),
    ("O4", "2026-10-01", "R2", "Course B", "North", "Public", 1, 7000.0),  # FY2027
    ("O5", "2025-09-30", "R1", "Course A", "Bangkok", "Public", 1, 900.0),  # FY2025
]
TARGETS = [("R1", 2026, 10000.0), ("R2", 2026, 6000.0), ("R1", 2027, 12000.0), ("R2", 2027, 8000.0)]


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA marts")
    con.execute(
        """
        CREATE TABLE marts.dim_date AS
        SELECT CAST(strftime(d, '%Y%m%d') AS INTEGER) AS date_key, CAST(d AS DATE) AS date_day,
               year(d) + CASE WHEN month(d) >= 10 THEN 1 ELSE 0 END AS fiscal_year,
               'FY' || (year(d) + CASE WHEN month(d) >= 10 THEN 1 ELSE 0 END) AS fiscal_year_label,
               ((month(d) + 2) % 12) + 1 AS fiscal_month_number
        FROM range(DATE '2024-10-01', DATE '2027-10-01', INTERVAL 1 DAY) t(d)
        """
    )
    con.execute("CREATE TABLE marts.dim_sales_rep (rep_id VARCHAR, rep_name VARCHAR)")
    con.execute("INSERT INTO marts.dim_sales_rep VALUES ('R1', 'Alice'), ('R2', 'Bob')")
    con.execute("CREATE TABLE marts.dim_course AS SELECT md5(n) AS course_id, n AS course_name FROM (VALUES ('Course A'), ('Course B')) v(n)")
    con.execute(
        """CREATE TABLE marts.fact_sales (order_id VARCHAR, sale_date DATE, date_key INTEGER, rep_id VARCHAR,
           course_id VARCHAR, region VARCHAR, channel_type VARCHAR, quantity INTEGER,
           unit_price DECIMAL(6,1), amount DECIMAL(6,1))"""
    )
    for order_id, day, rep, course, region, channel, qty, price in SALES:
        con.execute(
            "INSERT INTO marts.fact_sales VALUES (?, CAST(? AS DATE), CAST(strftime(CAST(? AS DATE), '%Y%m%d') AS INTEGER), ?, md5(?), ?, ?, ?, ?, ?)",
            [order_id, day, day, rep, course, region, channel, qty, price, qty * price],
        )
    con.execute("CREATE TABLE marts.fact_sales_target (target_id VARCHAR, rep_id VARCHAR, fiscal_year INTEGER, annual_target DOUBLE)")
    for rep, fy, target in TARGETS:
        con.execute("INSERT INTO marts.fact_sales_target VALUES (?, ?, ?, ?)", [f"{rep}-{fy}", rep, fy, target])
    con.close()
    monkeypatch.setenv("DASHBOARD_DUCKDB_PATH", str(path))
    return path


def test_rep_vs_target_and_kpis(warehouse):
    lines, targets = data.load_sales_lines(), data.load_targets()
    reps = data.rep_vs_target(lines, targets, 2026)
    assert reps[["rep_name", "actual", "target", "pct_of_target"]].values.tolist() == [
        ["Alice", 7000.0, 10000.0, 70.0],
        ["Bob", 3000.0, 6000.0, 50.0],
    ]
    k = data.kpis(data.filter_lines(lines, 2026), reps)
    assert k == {"revenue": 10000.0, "orders": 3, "seats": 6, "target": 16000.0, "pct_of_target": 62.5}


def test_filters_and_breakdowns(warehouse):
    lines = data.load_sales_lines()
    bangkok = data.filter_lines(lines, 2026, regions=["Bangkok"])
    assert sorted(bangkok["order_id"]) == ["O1", "O3"]
    by_course = data.revenue_by(data.filter_lines(lines, 2026), "course_name")
    assert sorted(by_course[["course_name", "revenue"]].values.tolist()) == [["Course A", 5000.0], ["Course B", 5000.0]]


def test_monthly_revenue_has_all_twelve_fiscal_months(warehouse):
    lines, years = data.load_sales_lines(), data.load_fiscal_years()
    monthly = data.monthly_revenue(data.filter_lines(lines, 2026), years, 2026)
    assert len(monthly) == 12
    assert monthly["month_start"].iloc[0].strftime("%Y-%m") == "2025-10"  # fiscal year starts in October
    assert monthly["month_start"].iloc[-1].strftime("%Y-%m") == "2026-09"
    assert monthly["revenue"].sum() == 10000.0 and (monthly["revenue"] == 0).sum() == 9


def test_default_fiscal_year_is_latest_finished_year(warehouse):
    lines, years = data.load_sales_lines(), data.load_fiscal_years()
    assert data.default_fiscal_year(years, lines, date(2026, 11, 24)) == 2026  # FY2027 still running
    assert data.default_fiscal_year(years, lines, date(2027, 12, 1)) == 2027


def test_connection_is_released_after_each_query(warehouse):
    data.load_sales_lines()
    con = duckdb.connect(str(warehouse))  # a writer (like dbt) can open the file right away
    con.close()


def test_app_renders_and_filters(warehouse):
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception and not at.error
    assert at.selectbox(key="fy").value in (2026, 2027)
    at.selectbox(key="fy").set_value(2026).run()
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Revenue"] == "฿10,000" and metrics["Target"] == "฿16,000" and metrics["% of target"] == "62.5%"
    at.multiselect(key="regions").set_value(["North"]).run()
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Revenue"] == "฿5,000" and metrics["Target"] == "฿16,000"  # target ignores region
    assert not at.exception


def test_app_reports_missing_warehouse(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DUCKDB_PATH", str(tmp_path / "missing.duckdb"))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert at.error and "not found" in at.error[0].value


def test_app_reports_locked_warehouse(warehouse):
    # Another process holds a read-write connection, like a running dbt build or DBeaver.
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import duckdb, sys, time; c = duckdb.connect(sys.argv[1]); print('ready', flush=True); time.sleep(60)",
         str(warehouse)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        at = AppTest.from_file(str(APP), default_timeout=60).run()
        assert not at.exception
        assert at.error and "locked" in at.error[0].value
    finally:
        holder.kill()
        holder.wait()
```

The tests build their own small warehouse, so they need no dbt run. Without Streamlit installed they skip themselves.

### 11.3 Hosting (Docker + Cloudflare Tunnel + Cloudflare Access)

```text
browser ─► Cloudflare Access (login) ─► Cloudflare Tunnel ─► tunnel container ─► dashboard:8501
```

The hostname, server and tunnel ID below are this deployment's values (`agentic-dashboard.praponb.com`, `prapon@192.168.1.49`); replace them for your own. Order matters: **create the Access application before the DNS record**, so the hostname is never reachable without a login.

1. **Access:** Cloudflare Zero Trust → Access → Applications → Self-hosted, destination `<hostname>`, policy *Allow* with *Include → Emails = <you>*.
2. **Tunnel and DNS** (from a machine where `cloudflared tunnel login` was run):
   ```bash
   cloudflared tunnel create --credentials-file deploy/cloudflared/<id>.json agentic-dashboard
   cloudflared tunnel --config deploy/cloudflared/config.yml route dns <id> <hostname>
   ```
   Put the ID into `deploy/cloudflared/config.yml`. Keep the credentials file out of git. Always pass `--config` and the ID: if `~/.cloudflared/config.yml` names another tunnel, `route dns <name> <host>` routes to *that* tunnel.
3. **Deploy:** `deploy/deploy.sh` copies the project to the server (no venv, data or secrets), sends the credentials file with mode 600, builds the image and starts both containers. Re-run it to update.

**`Dockerfile.dashboard`**

```dockerfile
# Sales dashboard image (agentic-dashboard.praponb.com).
#
# Stage "warehouse" builds the data: it generates the mock data lake and runs `dbt build` on
# the committed models, so the image build FAILS if any dbt test fails. dbt stays in this stage.
# Stage "deps" installs only the `dashboard` dependency group (Streamlit + DuckDB, no dbt/MCP).
# Stage "runtime" runs Streamlit as a non-root user with the built warehouse.duckdb (read-only).
#
#   docker build -f Dockerfile.dashboard -t agentic-dashboard .
#   (normally via deploy/docker-compose.yml)

FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

FROM python:3.12.13-slim AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DO_NOT_TRACK=1 \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./


FROM base AS warehouse
# Full project environment (dbt), cached until pyproject.toml / uv.lock change.
RUN UV_PROJECT_ENVIRONMENT=/opt/dbt-venv uv sync --frozen --no-dev
COPY dbt_project.yml profiles.yml generate_mock_lake.py ./
COPY macros/ macros/
COPY models/ models/
COPY data_tests/ data_tests/
RUN /opt/dbt-venv/bin/python generate_mock_lake.py \
 && DUCKDB_PATH=/app/warehouse.duckdb /opt/dbt-venv/bin/dbt build --profiles-dir . \
 && /opt/dbt-venv/bin/python -c "import duckdb; c = duckdb.connect('/app/warehouse.duckdb', read_only=True); \
print('marts tables:', [r[0] for r in c.execute(\"select table_name from duckdb_tables() where schema_name = 'marts' order by 1\").fetchall()])"


FROM base AS deps
# Dashboard-only environment: same locked versions, without dbt or the MCP server packages.
RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-dev --only-group dashboard


FROM python:3.12.13-slim AS runtime
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin dashboard
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY --from=warehouse --chmod=444 /app/warehouse.duckdb /app/warehouse.duckdb
COPY dashboard/ dashboard/
COPY .streamlit/ .streamlit/

ENV PATH=/app/.venv/bin:$PATH \
    HOME=/tmp \
    TZ=Asia/Bangkok \
    PYTHONDONTWRITEBYTECODE=1 \
    DASHBOARD_DUCKDB_PATH=/app/warehouse.duckdb \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_SERVER_ADDRESS=agentic-dashboard.praponb.com \
    STREAMLIT_BROWSER_SERVER_PORT=443 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

USER 10001
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4).read() == b'ok' else 1)"]
CMD ["streamlit", "run", "dashboard/app.py"]
```

**`.dockerignore`**

```text
# Keep the build context small and free of local data and secrets.
.venv/
**/__pycache__/
.pytest_cache/
target/
logs/
dbt_packages/
data_lake/
warehouse.duckdb
warehouse.duckdb.wal
.env
docker/
deploy/
.mcp.json
.user.yml
tests/
```

**`deploy/docker-compose.yml`**

```yaml
name: agentic-dashboard

# Sales dashboard at https://agentic-dashboard.praponb.com
#   dashboard: Streamlit (image builds the warehouse with dbt; see ../Dockerfile.dashboard)
#   tunnel:    dedicated Cloudflare Tunnel -> http://dashboard:8501 (no inbound ports needed)
# Cloudflare Access (login) protects the hostname; the app itself has no row-level security.
# Deploy from the Mac with deploy/deploy.sh. deploy/.env holds HOST_UID/HOST_GID (written by it).

services:
  dashboard:
    build:
      context: ..
      dockerfile: Dockerfile.dashboard
    image: agentic-dashboard:latest
    ports:
      - "127.0.0.1:8502:8501"   # on-server checks only (8501 is used by another app)
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    restart: unless-stopped

  tunnel:
    image: cloudflare/cloudflared:2026.11.2
    command: ["tunnel", "--no-autoupdate", "--config", "/etc/cloudflared/config.yml", "run"]
    user: "${HOST_UID:?run deploy/deploy.sh}:${HOST_GID:?run deploy/deploy.sh}"  # reads the 600-mode credentials file
    volumes:
      - ./cloudflared:/etc/cloudflared:ro
    read_only: true
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    depends_on:
      dashboard:
        condition: service_healthy
    restart: unless-stopped
```

**`deploy/cloudflared/config.yml`**

```yaml
# Cloudflare Tunnel "agentic-dashboard" (runs in the `tunnel` service of deploy/docker-compose.yml).
# The tunnel ID is not a secret. The credentials file <ID>.json is git-ignored and copied to
# the server by deploy/deploy.sh with mode 600.
# Cloudflare Access app "agentic-dashboard" (policy "Owner only") protects this hostname.
tunnel: f0316847-fd31-462f-9306-4f0dfb354175
credentials-file: /etc/cloudflared/f0316847-fd31-462f-9306-4f0dfb354175.json
no-autoupdate: true

ingress:
  - hostname: agentic-dashboard.praponb.com
    service: http://dashboard:8501
  - service: http_status:404
```

**`deploy/deploy.sh`**

```bash
#!/usr/bin/env bash
# Deploy the sales dashboard to the Ubuntu server and (re)start it.
#
#   agentic_analytics_demo/deploy/deploy.sh            # default host prapon@192.168.1.49
#   DEPLOY_HOST=user@host deploy/deploy.sh
#
# Copies the project (no local data, venv or secrets) to ~/agentic-dashboard on the server,
# sends the tunnel credentials file with mode 600, then builds and starts the containers.
# The image build runs `dbt build`, so a failing dbt test stops the deploy.
set -euo pipefail

HOST="${DEPLOY_HOST:-prapon@192.168.1.49}"
REMOTE_DIR="${DEPLOY_DIR:-agentic-dashboard}"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$PROJECT/deploy/cloudflared/config.yml"

TUNNEL_ID="$(awk '/^tunnel:/ {print $2}' "$CONFIG")"
CREDS="$PROJECT/deploy/cloudflared/$TUNNEL_ID.json"
[ -f "$CREDS" ] || CREDS="$HOME/.cloudflared/$TUNNEL_ID.json"
[ -f "$CREDS" ] || { echo "Tunnel credentials $TUNNEL_ID.json not found (deploy/cloudflared/ or ~/.cloudflared/)." >&2; exit 1; }

echo "==> Copying project to $HOST:~/$REMOTE_DIR"
rsync -az --delete \
  --exclude .venv/ --exclude '__pycache__/' --exclude .pytest_cache/ --exclude target/ \
  --exclude logs/ --exclude dbt_packages/ --exclude data_lake/ --exclude 'warehouse.duckdb*' \
  --exclude .env --exclude docker/.env --exclude .mcp.json --exclude .user.yml \
  --exclude 'deploy/.env' --exclude 'deploy/cloudflared/*.json' \
  "$PROJECT/" "$HOST:$REMOTE_DIR/"

echo "==> Sending tunnel credentials (mode 600)"
ssh "$HOST" "umask 077 && cat > $REMOTE_DIR/deploy/cloudflared/$TUNNEL_ID.json && chmod 600 $REMOTE_DIR/deploy/cloudflared/$TUNNEL_ID.json" < "$CREDS"

echo "==> Building and starting containers (runs dbt build; first run takes a few minutes)"
ssh "$HOST" "cd $REMOTE_DIR/deploy \
  && printf 'HOST_UID=%s\nHOST_GID=%s\n' \"\$(id -u)\" \"\$(id -g)\" > .env \
  && docker compose up -d --build --remove-orphans \
  && for i in \$(seq 1 30); do \
       [ \"\$(docker inspect -f '{{.State.Health.Status}}' agentic-dashboard-dashboard-1 2>/dev/null)\" = healthy ] && break; sleep 2; \
     done \
  && docker compose ps --format '{{.Service}}: {{.State}} {{.Status}}' \
  && echo -n 'health: ' && curl -s http://127.0.0.1:8502/_stcore/health && echo"
```

**Checks after deploy:**
- On the server: `docker compose ps` shows the dashboard as `healthy`; `curl -s 127.0.0.1:8502/_stcore/health` prints `ok`; `docker compose logs tunnel` shows 4 *Registered tunnel connection* lines.
- Without a login, `curl -sI https://<hostname>/_stcore/health` returns `302` to `<team>.cloudflareaccess.com`, never `ok`.
- After logging in, the page shows the Section 10 step 8 figures.

---

## 12. Known limitations

- **MCP SDK 2.x:** this server targets the 1.x API (`mcp.server.fastmcp`). Moving to 2.x means migrating to `mcp.server.mcpserver.MCPServer`.
- **Python:** verified on 3.12 and 3.11; `requires-python` is capped at `<3.13` until the suite is run on newer versions.
- **No RLS in the MCP query tool:** anyone with access to the MCP server can read all warehouse and lake data.
- **Single writer:** DuckDB allows one writing process. Close the DuckDB CLI/DBeaver (or a second MCP server) before `run_dbt_models`; conflicts are reported as `lock_conflict`.
- **Relative lake paths:** staging views store `read_parquet('data_lake/…')`. Tools other than this server must open `warehouse.duckdb` with the project root as working directory (or use `materialized='table'` for those models).
- **CSV encoding:** lake CSVs must be UTF-8. Convert legacy Thai encodings (TIS-620 / CP874) before landing files.
- **DuckDB 1.x info pragmas:** read-only pragmas such as `PRAGMA version` or `PRAGMA table_info(...)` are accepted on DuckDB 1.x (the parser rewrites them to SELECT) and rejected on 2.x. State-changing pragmas are rejected on both.
- **Cube SQL API coverage:** Cube's SQL layer does not support every SQL shape. Joining two hand-written subqueries (`… JOIN (SELECT … FROM SalesReps) r ON …`) fails with `Use __cubeJoinField to join Cubes`, and `WHERE x = 'a' OR 1=1` fails with a rewrite error, for admins too. Query across cubes by selecting members of joined cubes instead (`SELECT SalesReps.rep_name, MEASURE(Sales.total_revenue) FROM Sales CROSS JOIN SalesReps GROUP BY 1`).
- **Unprotected cubes:** `Courses` is not filtered by RLS (the course list is not sensitive). Add a cube to `RLS_MEMBERS` in `cube.js` if it must be filtered.
- **Direct Postgres access bypasses RLS:** Postgres is published on `127.0.0.1:5432` for the publish script. Anyone using that port reads all rows.
- **Enterprise stack tested on one machine:** Docker 29.9.3 on macOS (arm64). Re-run `check_enterprise_stack.py` after upgrading image tags.
- **Dashboard has no RLS:** the Section 11 page shows every row to whoever can open it. Its only protection when hosted is the Cloudflare Access login.
- **Hosted dashboard data is rebuilt per deploy:** the image builds the mock lake and warehouse at build time, so the hosted page changes only when `deploy/deploy.sh` runs. Pointing it at real data needs a volume with a warehouse file and a refresh schedule instead.
