-- The fact table's natural key must be unique; duplicates mean the staging
-- upsert broke. Returning rows = test failure.
select source, campaign_id, stat_date, count(*) as n
from {{ ref('fact_ad_performance_daily') }}
group by 1, 2, 3
having count(*) > 1
