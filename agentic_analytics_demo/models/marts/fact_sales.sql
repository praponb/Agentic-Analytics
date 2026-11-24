{{ config(materialized='table') }}

-- Grain: one row per order line
select
    order_id,
    sale_date,
    cast(strftime(sale_date, '%Y%m%d') as integer) as date_key,
    rep_id,
    md5(lower(trim(course_name))) as course_id,
    region,
    channel_type,
    quantity,
    unit_price,
    amount
from {{ ref('stg_data_lake__raw_sales_2026') }}
