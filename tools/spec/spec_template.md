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

<<FILE:pyproject.toml:toml>>

### .python-version

<<FILE:.python-version:text>>

### .gitignore

<<FILE:.gitignore:text>>

---

## 5. dbt project files

### dbt_project.yml

<<FILE:dbt_project.yml:yaml>>

### profiles.yml

<<FILE:profiles.yml:yaml>>

### macros/generate_schema_name.sql

Produces clean schema names (`staging`, `marts`) instead of dbt's default `main_staging` / `main_marts`.

<<FILE:macros/generate_schema_name.sql:sql>>

### generate_mock_lake.py

Generates `data_lake/raw_sales_2026.parquet` (with a spaced column name, `"Order ID"`, to exercise snake_case normalisation; 24 rows spanning FY2025–FY2027 for an October fiscal start) and `data_lake/sales_targets.csv` (FY2026 and FY2027 targets). DuckDB only — no pandas/pyarrow. Works from any CWD.

<<FILE:generate_mock_lake.py:python>>

### Starter marts (`starter_marts/`)

Star schema at order-line grain. `course_id` is a stable surrogate key (`md5` of the normalised course name).

<<FILE:starter_marts/dim_sales_rep.sql:sql>>

<<FILE:starter_marts/dim_course.sql:sql>>

<<FILE:starter_marts/fact_sales.sql:sql>>

<<FILE:starter_marts/fact_sales_target.sql:sql>>

<<FILE:starter_marts/marts_models.yml:yaml>>

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

<<FILE:mcp_server/dbt_mcp_server.py:python>>

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

<<FILE:docker/docker-compose.yml:yaml>>

### docker/.env.example

<<FILE:docker/.env.example:bash>>

### docker/cube_schema/model/sales.yml

<<FILE:docker/cube_schema/model/sales.yml:yaml>>

### docker/cube_schema/cube.js

<<FILE:docker/cube_schema/cube.js:javascript>>

### scripts/publish_marts_to_postgres.py

<<FILE:scripts/publish_marts_to_postgres.py:python>>

### scripts/check_enterprise_stack.py

<<FILE:scripts/check_enterprise_stack.py:python>>

### docker/cube_rls_unit_check.js

<<FILE:docker/cube_rls_unit_check.js:javascript>>

---

## 8. MCP client configuration

`.mcp.json` must contain absolute paths for the current machine, so it is **generated** (and git-ignored) instead of committed:

```bash
uv run python scripts/write_mcp_config.py
```

The script writes `.mcp.json` for Claude Code (project scope) and prints the same block for Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows). If a long `dbt run` exceeds your MCP client's tool-call timeout, raise the client timeout or lower `DBT_TIMEOUT_SEC` and run large builds from the terminal.

### scripts/write_mcp_config.py

<<FILE:scripts/write_mcp_config.py:python>>

### .mcp.json.example

<<FILE:.mcp.json.example:json>>

---

## 9. Automated verification suite — `tests/test_mcp_server.py`

Claude Code MUST create and run this suite. It copies the project into a temp directory, so it never touches the real models or warehouse. It covers the end-to-end pipeline, fiscal-year boundaries, every sandbox bypass found in the v1.0 review, statement classification, query timeout, stale run results, YAML merge, cross-process lock reporting, and the MCP protocol over STDIO with the server launched from `/`.

<<FILE:tests/test_mcp_server.py:python>>

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

<<PFILE:.streamlit/config.toml:toml>>

<<PFILE:dashboard/data.py:python>>

<<PFILE:dashboard/app.py:python>>

<<PFILE:tests/test_dashboard.py:python>>

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

<<PFILE:Dockerfile.dashboard:dockerfile>>

<<PFILE:.dockerignore:text>>

<<PFILE:deploy/docker-compose.yml:yaml>>

<<PFILE:deploy/cloudflared/config.yml:yaml>>

<<PFILE:deploy/deploy.sh:bash>>

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
