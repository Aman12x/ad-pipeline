select *
from {{ ref('fact_ad_performance_daily') }}
where least(impressions, clicks, spend, platform_conversions,
            fp_conversions, fp_revenue) < 0
