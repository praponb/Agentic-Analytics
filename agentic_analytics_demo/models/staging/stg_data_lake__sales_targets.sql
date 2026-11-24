{{
    config(
        materialized='view'
    )
}}

with source as (

    select * from {{ source('data_lake', 'sales_targets') }}

),

renamed as (

    select
        rep_id,
        rep_name,
        fiscal_year,
        annual_target
    from source

)

select * from renamed
