"""Local sales dashboard (agentic-dashboard.praponb.com, local preview).

Run from the project root:
    uv sync --group dashboard
    uv run --group dashboard streamlit run dashboard/app.py

Listens on http://127.0.0.1:8501 only (see .streamlit/config.toml): it shows every row in
the warehouse, with no row-level security. For per-user dashboards use Metabase via Cube.
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
st.caption("agentic-dashboard.praponb.com · local preview · sales from the dbt marts in warehouse.duckdb")

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
    "No row-level security: this page shows every row, so it only listens on 127.0.0.1."
)
