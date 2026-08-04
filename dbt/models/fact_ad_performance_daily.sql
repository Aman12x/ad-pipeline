-- Platform-reported stats joined to first-party truth at (source, campaign, day).
-- platform_conversions and fp_* stay separate columns on purpose: platforms
-- over-attribute, and the gap between the two IS the signal, never to be summed.

with fp as (
    select
        utm_source   as source,
        utm_campaign as campaign_id,
        order_date   as stat_date,
        count(*)                 as fp_conversions,
        round(sum(revenue), 2)   as fp_revenue
    from {{ source('pipeline', 'stg_orders') }}
    group by 1, 2, 3
)

select
    s.source,
    s.campaign_id,
    s.campaign_name,
    s.stat_date,
    s.impressions,
    s.clicks,
    s.spend,
    s.platform_conversions,
    coalesce(fp.fp_conversions, 0)   as fp_conversions,
    coalesce(fp.fp_revenue, 0.0)     as fp_revenue
from {{ source('pipeline', 'stg_ad_stats') }} s
left join fp using (source, campaign_id, stat_date)
