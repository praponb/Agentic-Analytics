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
