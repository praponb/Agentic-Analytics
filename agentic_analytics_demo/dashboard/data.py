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
