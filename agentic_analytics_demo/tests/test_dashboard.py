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
