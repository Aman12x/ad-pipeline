select
    source,
    stat_date,
    sum(impressions)                                             as impressions,
    sum(clicks)                                                  as clicks,
    round(sum(spend), 2)                                         as spend,
    round(sum(platform_conversions), 2)                          as platform_conversions,
    sum(fp_conversions)                                          as fp_conversions,
    round(sum(fp_revenue), 2)                                    as fp_revenue,
    round(sum(spend) / nullif(sum(fp_conversions), 0), 2)        as cac,
    round(sum(fp_revenue) / nullif(sum(spend), 0), 2)            as roas
from {{ ref('fact_ad_performance_daily') }}
group by 1, 2
