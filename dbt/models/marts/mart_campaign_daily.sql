select
    source,
    campaign_id,
    campaign_name,
    stat_date,
    impressions,
    clicks,
    spend,
    platform_conversions,
    fp_conversions,
    fp_revenue,
    round(clicks / nullif(impressions, 0), 4)                    as ctr,
    round(spend / nullif(fp_conversions, 0), 2)                  as cac,
    round(fp_revenue / nullif(spend, 0), 2)                      as roas,
    round(platform_conversions / nullif(fp_conversions, 0), 2)   as overclaim_ratio
from {{ ref('fact_ad_performance_daily') }}
