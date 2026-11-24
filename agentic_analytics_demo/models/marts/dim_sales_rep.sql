{{ config(materialized='table') }}

-- One row per sales rep, from both transactions and targets
with reps as (

    select rep_id, rep_name from {{ ref('stg_data_lake__raw_sales_2026') }}
    union
    select rep_id, rep_name from {{ ref('stg_data_lake__sales_targets') }}

)

select
    rep_id,
    max(rep_name) as rep_name
from reps
group by rep_id
