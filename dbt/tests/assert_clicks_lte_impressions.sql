select *
from {{ ref('fact_ad_performance_daily') }}
where clicks > impressions
