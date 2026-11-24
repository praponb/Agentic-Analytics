# Agentic Analytics (dbt + DuckDB + FastMCP)

[![Tests](https://github.com/praponb/Agentic-Analytics/actions/workflows/tests.yml/badge.svg)](https://github.com/praponb/Agentic-Analytics/actions/workflows/tests.yml)

An open-source blueprint for "chat with your data" and AI-assisted data modelling, without Power BI or Microsoft Fabric. An AI client such as Claude Code or Claude Desktop talks to an MCP server that can explore a local data lake, query a DuckDB warehouse, scaffold dbt models and run dbt tests. An optional Docker stack (Postgres, Cube, Metabase) adds a semantic layer with row-level security for dashboards.

## What's in this repo

| Path | What it is |
|---|---|
| [`Agentic Analytics Spec v1.1.md`](Agentic%20Analytics%20Spec%20v1.1.md) | **The spec to use.** Corrected and tested implementation spec. Hand this file to Claude Code. Section 0 lists every fix made to v1.0. |
| [`reference/agentic_analytics_demo/`](reference/agentic_analytics_demo/) | A working copy of every file in the spec. The spec's code blocks are generated from these files, so the two always match. |
| [`agentic_analytics_demo/`](agentic_analytics_demo/) | The project implemented from the spec (Section 10 checklist), including the dbt models the MCP tools generated, plus a local sales dashboard (see below). |
| `Agentic Analytics (dbt + DuckDB + FastMCP) Spec.docx` | The original v1.0 spec, kept for reference only. It has known blocking problems (see Section 0 of v1.1); do not implement from it. |

## Verification status

| Part | Status |
|---|---|
| Local core: dbt + DuckDB + MCP server | Tested. `pytest` 47/47 on DuckDB 2.0.1, 1.4.4 and 1.3.2 with Python 3.12, and on Python 3.11; `dbt build` 66/66; MCP protocol tested over STDIO. GitHub Actions re-runs this on every push (see below). |
| Enterprise stack: Postgres + Cube + Metabase | Tested live on Docker (macOS, arm64) in both dev and production mode. Row-level security checked through the Cube REST API, the Cube SQL API and Metabase. |

## Quick start (local core)

Needs [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself if needed.

```bash
cd reference/agentic_analytics_demo
uv sync                                    # install dependencies from uv.lock
uv run python generate_mock_lake.py        # create sample data in data_lake/
uv run dbt debug --profiles-dir .          # check the dbt setup
uv run pytest -v                           # expect 47 passed
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

## Sales dashboard (https://agentic-dashboard.praponb.com)

A small Streamlit app in [`agentic_analytics_demo/dashboard/`](agentic_analytics_demo/dashboard/) that reads the dbt marts in `warehouse.duckdb` directly. No Docker needed.

```bash
cd agentic_analytics_demo
uv sync --group dashboard                                  # adds Streamlit (optional group)
uv run --group dashboard streamlit run dashboard/app.py    # then open http://localhost:8501
```

- **Shows:** revenue, orders, seats, target and % of target; actual vs. target by rep; revenue by month across the fiscal year (October to September); revenue by course, region and channel; the order lines, with a CSV download.
- **Filters:** fiscal year (defaults to the latest finished one), sales rep, region, channel and course. Targets exist per rep and fiscal year, so the target figures ignore the region, channel and course filters.
- **Data:** each refresh opens `warehouse.duckdb` read-only for a moment, so it doesn't block `dbt` runs. If a writer holds the file, the page says so; refresh once it's done. Set `DASHBOARD_DUCKDB_PATH` to read another warehouse file.
- **Security:** no row-level security. Everyone who can open the page sees every row, so locally it listens on `127.0.0.1` only (`.streamlit/config.toml`), and the hosted copy sits behind a Cloudflare Access login. Use Metabase via Cube for per-user dashboards.

### Hosting

The hosted copy runs on the Ubuntu server `192.168.1.49` as the Docker Compose project `agentic-dashboard` (in `~/agentic-dashboard`). Nothing listens on a public port: a dedicated Cloudflare Tunnel carries the traffic.

```
browser ─► Cloudflare Access (login: "Owner only" policy) ─► Tunnel "agentic-dashboard" ─► tunnel container ─► dashboard:8501
```

- **Image:** [`Dockerfile.dashboard`](agentic_analytics_demo/Dockerfile.dashboard). Its build stage generates the mock lake and runs `dbt build`, so **a failing dbt test fails the deploy**. The runtime stage runs Streamlit as a non-root user on a read-only filesystem with only `warehouse.duckdb`.
- **Compose:** [`deploy/docker-compose.yml`](agentic_analytics_demo/deploy/docker-compose.yml), with the `dashboard` and `tunnel` services. The dashboard is also on `127.0.0.1:8502` on the server for checks, because 8501 is taken there.
- **Tunnel:** [`deploy/cloudflared/config.yml`](agentic_analytics_demo/deploy/cloudflared/config.yml). The credentials file `deploy/cloudflared/<tunnel-id>.json` is git-ignored. `deploy.sh` copies it to the server with mode 600.
- **DNS:** a proxied CNAME `agentic-dashboard` → `<tunnel-id>.cfargotunnel.com` in the `praponb.com` zone.
- **Login:** Cloudflare Zero Trust → Access → Applications → `agentic-dashboard`, using the reusable "Owner only" policy.

Deploy or update from the Mac (copies the project, rebuilds the image, restarts, waits for healthy):

```bash
agentic_analytics_demo/deploy/deploy.sh                 # DEPLOY_HOST=user@host to override
```

Operate on the server:

```bash
cd ~/agentic-dashboard/deploy
docker compose ps                     # both services up, dashboard (healthy)
docker compose logs -f tunnel         # "Registered tunnel connection" x4
curl -s 127.0.0.1:8502/_stcore/health # ok
docker compose down                   # take the site offline (Access still blocks the hostname)
```

Remove completely: `docker compose down --rmi local` on the server, then delete the DNS record and the Access application in Cloudflare, and run `cloudflared tunnel delete agentic-dashboard` on the Mac.

> **Gotcha:** if `~/.cloudflared/config.yml` names another tunnel, `cloudflared tunnel route dns <name> <host>` routes to **that** tunnel. Pass the config explicitly: `cloudflared tunnel --config agentic_analytics_demo/deploy/cloudflared/config.yml route dns --overwrite-dns <tunnel-id> agentic-dashboard.praponb.com`.

## Continuous integration

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs on every push to `main` or `b_pbv_main`, on pull requests and on demand:
- **pytest:** the full suite on Ubuntu with Python 3.12 + DuckDB 2.0.1, 1.4.4 and 1.3.2, and Python 3.11 + DuckDB 2.0.1.
- **Docker stack (static):** `docker compose config` and the `cube.js` row-level security unit checks. CI does not start the containers; run `scripts/check_enterprise_stack.py` locally for the live check.
- **Dashboard:** `agentic_analytics_demo/tests/test_dashboard.py` (8 tests: figures, filters, fiscal months, missing or locked warehouse, the app rendering headless).
- **Dashboard image:** builds `Dockerfile.dashboard` (including `dbt build`) and checks that the container starts healthy, without the tunnel.

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

## License

[MIT](LICENSE)
