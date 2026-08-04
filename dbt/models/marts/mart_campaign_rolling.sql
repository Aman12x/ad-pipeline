-- Rolling 7-day efficiency per campaign, plus a creative-fatigue signal.
-- Excludes the most recent day (first-party orders land with a 1-day lag,
-- so its CAC/ROAS would be computed against zero orders).
--
-- ctr_vs_peak: the campaign's rolling CTR as a fraction of its own best
-- rolling CTR. A steady slide below ~0.8 on a campaign that isn't new is the
-- classic creative-fatigue pattern -> refresh the ads.

with mature as (
    select *
    from {{ ref('fact_ad_performance_daily') }}
    where stat_date < (select max(stat_date)
                       from {{ ref('fact_ad_performance_daily') }})
),

daily as (
    select
        *,
        min(stat_date) over (partition by source, campaign_id) as campaign_start
    from mature
),

rolling as (
    select
        source,
        campaign_id,
        campaign_name,
        stat_date,
        date_diff('day', campaign_start, stat_date) as campaign_age_days,
        sum(spend)           over w7 as spend_7d,
        sum(fp_conversions)  over w7 as fp_conversions_7d,
        sum(fp_revenue)      over w7 as fp_revenue_7d,
        sum(clicks)          over w7 as clicks_7d,
        sum(impressions)     over w7 as impressions_7d
    from daily
    window w7 as (
        partition by source, campaign_id
        order by stat_date
        rows between 6 preceding and current row
    )
)

select
    source,
    campaign_id,
    campaign_name,
    stat_date,
    campaign_age_days,
    round(spend_7d, 2)                                       as spend_7d,
    fp_conversions_7d,
    round(spend_7d / nullif(fp_conversions_7d, 0), 2)        as cac_7d,
    round(fp_revenue_7d / nullif(spend_7d, 0), 2)            as roas_7d,
    round(clicks_7d / nullif(impressions_7d, 0), 4)          as ctr_7d,
    round(
        (clicks_7d / nullif(impressions_7d, 0))
        / nullif(max(clicks_7d / nullif(impressions_7d, 0))
                     over (partition by source, campaign_id), 0),
        3)                                                   as ctr_vs_peak
from rolling
