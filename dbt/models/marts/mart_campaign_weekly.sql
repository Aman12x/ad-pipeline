-- Week-over-week movement per campaign, with each campaign's share of its
-- channel's total spend change -- the "which campaign drove the change"
-- decomposition. share_of_channel_spend_change sums to 1.0 within a
-- (source, week) whenever the channel's spend moved at all.

with mature as (
    select *
    from {{ ref('fact_ad_performance_daily') }}
    where stat_date < (select max(stat_date)
                       from {{ ref('fact_ad_performance_daily') }})
),

weekly as (
    select
        source,
        campaign_id,
        campaign_name,
        date_trunc('week', stat_date) as week_start,
        count(*)                      as days_in_week,   -- partial weeks visible
        sum(spend)                    as spend,
        sum(fp_conversions)           as fp_conversions,
        sum(fp_revenue)               as fp_revenue
    from mature
    group by 1, 2, 3, 4
),

lagged as (
    select
        *,
        lag(spend) over w           as prev_spend,
        lag(fp_conversions) over w  as prev_fp_conversions
    from weekly
    window w as (partition by source, campaign_id order by week_start)
)

select
    source,
    campaign_id,
    campaign_name,
    week_start,
    days_in_week,
    round(spend, 2)                                          as spend,
    fp_conversions,
    round(spend / nullif(fp_conversions, 0), 2)              as cac,
    round(fp_revenue / nullif(spend, 0), 2)                  as roas,
    round(spend - prev_spend, 2)                             as wow_spend_change,
    round(spend / nullif(fp_conversions, 0)
          - prev_spend / nullif(prev_fp_conversions, 0), 2)  as wow_cac_change,
    round((spend - prev_spend)
          / nullif(sum(spend - prev_spend)
                       over (partition by source, week_start), 0),
          3)                                                 as share_of_channel_spend_change
from lagged
