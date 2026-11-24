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
