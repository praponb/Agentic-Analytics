{{ config(materialized='table') }}

-- Grain: one row per sales rep per fiscal year
select
    rep_id || '-' || fiscal_year::varchar as target_id,
    rep_id,
    fiscal_year::integer as fiscal_year,
    annual_target
from {{ ref('stg_data_lake__sales_targets') }}
