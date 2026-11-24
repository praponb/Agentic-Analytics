{{
    config(
        materialized='view'
    )
}}

with source as (

    select * from {{ source('data_lake', 'raw_sales_2026') }}

),

renamed as (

    select
        "Order ID" as order_id,
        sale_date,
        rep_id,
        rep_name,
        region,
        course_name,
        channel_type,
        quantity,
        unit_price,
        amount
    from source

)

select * from renamed
