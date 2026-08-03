"""Transform: rebuild the fact table and marts from staging.

Fact and marts are derived entirely from staging, so they're rebuilt wholesale
each run (CREATE OR REPLACE) -- trivially idempotent, and staging is small
enough that incremental fact builds aren't worth the complexity yet.

platform_conversions and fp_* stay separate columns on purpose: platforms
over-attribute, and the gap between the two IS the signal, never to be summed.
"""

import logging

import duckdb

log = logging.getLogger("pipeline.transform")

SQL = """
CREATE OR REPLACE TABLE fact_ad_performance_daily AS
WITH fp AS (
    SELECT utm_source AS source,
           utm_campaign AS campaign_id,
           order_date AS stat_date,
           count(*) AS fp_conversions,
           round(sum(revenue), 2) AS fp_revenue
    FROM stg_orders
    GROUP BY 1, 2, 3
)
SELECT s.source, s.campaign_id, s.campaign_name, s.stat_date,
       s.impressions, s.clicks, s.spend, s.platform_conversions,
       coalesce(fp.fp_conversions, 0) AS fp_conversions,
       coalesce(fp.fp_revenue, 0.0) AS fp_revenue
FROM stg_ad_stats s
LEFT JOIN fp USING (source, campaign_id, stat_date);

CREATE OR REPLACE TABLE mart_campaign_daily AS
SELECT source, campaign_id, campaign_name, stat_date,
       impressions, clicks, spend, platform_conversions,
       fp_conversions, fp_revenue,
       round(clicks / nullif(impressions, 0), 4)        AS ctr,
       round(spend / nullif(fp_conversions, 0), 2)      AS cac,
       round(fp_revenue / nullif(spend, 0), 2)          AS roas,
       round(platform_conversions / nullif(fp_conversions, 0), 2)
                                                        AS overclaim_ratio
FROM fact_ad_performance_daily;

CREATE OR REPLACE TABLE mart_channel_daily AS
SELECT source, stat_date,
       sum(impressions) AS impressions,
       sum(clicks) AS clicks,
       round(sum(spend), 2) AS spend,
       round(sum(platform_conversions), 2) AS platform_conversions,
       sum(fp_conversions) AS fp_conversions,
       round(sum(fp_revenue), 2) AS fp_revenue,
       round(sum(spend) / nullif(sum(fp_conversions), 0), 2) AS cac,
       round(sum(fp_revenue) / nullif(sum(spend), 0), 2)     AS roas
FROM fact_ad_performance_daily
GROUP BY 1, 2;
"""


def transform(con: duckdb.DuckDBPyConnection):
    con.execute(SQL)
    n = con.execute("SELECT count(*) FROM fact_ad_performance_daily").fetchone()[0]
    log.info("rebuilt fact (%d rows) + marts", n)
