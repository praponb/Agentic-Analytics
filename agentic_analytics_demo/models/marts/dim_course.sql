{{ config(materialized='table') }}

-- Stable surrogate key derived from the normalized course name
select
    md5(lower(trim(course_name))) as course_id,
    min(course_name) as course_name
from {{ ref('stg_data_lake__raw_sales_2026') }}
group by 1
