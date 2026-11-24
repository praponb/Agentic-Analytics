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
