-- Month-to-date spend vs. a prorated monthly budget (from the
-- monthly_budgets seed). pacing_ratio > 1 means spending ahead of plan;
-- the status bands give the "do I need to act today" answer directly.

with daily as (
    select source, stat_date, sum(spend) as spend
    from {{ ref('fact_ad_performance_daily') }}
    group by 1, 2
),

mtd as (
    select
        source,
        stat_date,
        strftime(stat_date, '%Y-%m') as month,
        spend,
        sum(spend) over (
            partition by source, strftime(stat_date, '%Y-%m')
            order by stat_date
        )                            as mtd_spend,
        day(stat_date)               as day_of_month,
        day(last_day(stat_date))     as days_in_month
    from daily
),

paced as (
    select
        m.source,
        m.stat_date,
        m.month,
        round(m.spend, 2)      as spend,
        round(m.mtd_spend, 2)  as mtd_spend,
        b.monthly_budget,
        round(b.monthly_budget * m.day_of_month / m.days_in_month, 2)
                               as expected_mtd_spend,
        round(m.mtd_spend
              / nullif(b.monthly_budget * m.day_of_month / m.days_in_month, 0),
              3)               as pacing_ratio
    from mtd m
    left join {{ ref('monthly_budgets') }} b using (source, month)
)

select
    *,
    case
        when pacing_ratio is null   then 'no_budget'
        when pacing_ratio > 1.10    then 'over'
        when pacing_ratio < 0.90    then 'under'
        else 'on_track'
    end as pacing_status
from paced
