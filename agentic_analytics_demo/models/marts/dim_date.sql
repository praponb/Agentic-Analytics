{{
    config(
        materialized='table'
    )
}}

with date_spine as (

    select
        unnest(
            generate_series(
                cast('2025-01-01' as date),
                cast('2027-12-31' as date),
                interval '1 day'
            )
        )::date as date_day

),

base_dates as (

    select
        cast(strftime(date_day, '%Y%m%d') as integer) as date_key,
        date_day,
        extract(year from date_day)::integer as calendar_year,
        extract(quarter from date_day)::integer as calendar_quarter,
        extract(month from date_day)::integer as month_number,
        strftime(date_day, '%B') as month_name,
        strftime(date_day, '%b') as month_short_name,
        strftime(date_day, '%Y-%m') as year_month,
        extract(day from date_day)::integer as day_of_month,
        extract(isodow from date_day)::integer as day_of_week,
        strftime(date_day, '%A') as day_name,
        strftime(date_day, '%a') as day_short_name,
        extract(isodow from date_day) in (6, 7) as is_weekend,
        case
            when extract(month from date_day) >= 10
                then extract(year from date_day)::integer + (1)
            else extract(year from date_day)::integer + (0)
        end as fiscal_year,
        (((extract(month from date_day)::integer - 10 + 12) % 12) + 1)::integer as fiscal_month_number,
        (floor(((extract(month from date_day)::integer - 10 + 12) % 12) / 3.0) + 1)::integer as fiscal_quarter
    from date_spine

),

final as (

    select
        date_key,
        date_day,
        calendar_year,
        calendar_quarter,
        'Q' || calendar_quarter::varchar as calendar_quarter_name,
        month_number,
        month_name,
        month_short_name,
        year_month,
        day_of_month,
        day_of_week,
        day_name,
        day_short_name,
        is_weekend,
        fiscal_year,
        'FY' || fiscal_year::varchar as fiscal_year_label,
        fiscal_quarter,
        'FY' || fiscal_year::varchar || '-Q' || fiscal_quarter::varchar as fiscal_quarter_label,
        fiscal_month_number,
        'FY' || fiscal_year::varchar || '-M' || lpad(fiscal_month_number::varchar, 2, '0') as fiscal_year_month
    from base_dates

)

select * from final
