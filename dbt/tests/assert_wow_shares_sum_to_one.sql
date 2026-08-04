-- Within each (source, week) where the channel's spend moved, campaign
-- shares of the change must sum to ~1.0 -- otherwise the decomposition
-- is broken. Rounding tolerance 0.02.
select source, week_start, sum(share_of_channel_spend_change) as total_share
from {{ ref('mart_campaign_weekly') }}
where share_of_channel_spend_change is not null
group by 1, 2
having abs(sum(share_of_channel_spend_change) - 1.0) > 0.02
