# Agentic Analytics (dbt + DuckDB + FastMCP)

An open-source blueprint for "chat with your data" and AI-assisted data modelling, without Power BI or Microsoft Fabric. An AI client such as Claude Code or Claude Desktop talks to an MCP server that can explore a local data lake, query a DuckDB warehouse, scaffold dbt models and run dbt tests. An optional Docker stack (Postgres, Cube, Metabase) adds a semantic layer with row-level security for dashboards.

## What's in this repo

| Path | What it is |
|---|---|
| [`Agentic Analytics Spec v1.1.md`](Agentic%20Analytics%20Spec%20v1.1.md) | **The spec to use.** Corrected and tested implementation spec. Hand this file to Claude Code. Section 0 lists every fix made to v1.0. |
| [`reference/agentic_analytics_demo/`](reference/agentic_analytics_demo/) | A working copy of every file in the spec. The spec's code blocks are generated from these files, so the two always match. |
| `Agentic Analytics (dbt + DuckDB + FastMCP) Spec.docx` | The original v1.0 spec, kept for reference only. It has known blocking problems (see Section 0 of v1.1); do not implement from it. |

## Verification status

| Part | Status |
|---|---|
| Local core: dbt + DuckDB + MCP server | Tested. `pytest` 46/46 on DuckDB 2.0.1, 1.4.4 and 1.3.2 with Python 3.12; `dbt build` 66/66; MCP protocol tested over STDIO. |
| Enterprise stack: Postgres + Cube + Metabase | Tested live on Docker (macOS, arm64) in both dev and production mode. Row-level security checked through the Cube REST API, the Cube SQL API and Metabase. |

## Quick start (local core)

Needs [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself if needed.

```bash
cd reference/agentic_analytics_demo
uv sync                                    # install dependencies from uv.lock
uv run python generate_mock_lake.py        # create sample data in data_lake/
uv run dbt debug --profiles-dir .          # check the dbt setup
uv run pytest -v                           # expect 46 passed
uv run python scripts/write_mcp_config.py  # register the MCP server, then restart Claude Code
```

Then try the live prompt in Section 10 of the spec. It should report FY2026 sales against target for 4 sales reps.

## Optional: enterprise stack

Needs Docker. Follow Section 7 of the spec. In short:

```bash
cd reference/agentic_analytics_demo
cp docker/.env.example docker/.env                  # then change every secret
docker compose -f docker/docker-compose.yml up -d
uv run python scripts/publish_marts_to_postgres.py  # copy marts to Postgres
uv run python scripts/check_enterprise_stack.py     # expect "ALL CHECKS PASSED"
```

Services listen on `127.0.0.1` only: Metabase on port 3000, Cube on 4000 (REST and Playground) and 15432 (SQL API).

## Security in one paragraph

The MCP query tool is for trusted analysts. It is read-only and its file access is limited to `data_lake/`, but it can read every row in the warehouse. Row-level security exists only in Cube: dashboards must connect through Cube (Metabase connects to Cube's SQL API), not directly to Postgres. Section 3 of the spec has the details.

## Fiscal years

Fiscal years start on 1 October and are named by the year they end in (October 2025 is FY2026), as in the Thai government. To name them by the start year instead, pass `fiscal_year_naming='start'` to `create_dim_date_model`.

## Known limitations

See Section 11 of the spec. The main ones:
- The MCP server targets MCP Python SDK 1.x.
- DuckDB allows only one writing process at a time.
- Lake CSV files must be UTF-8.
- Cube's SQL API cannot run every SQL shape.
