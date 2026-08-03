"""Unit tests for schema normalization and validation in pipeline.load."""

from datetime import date

from pipeline.load import _parse_google, _parse_meta, _parse_orders


def google_row(**overrides):
    row = {
        "campaign": {"id": "g-101", "name": "Brand Search"},
        "metrics": {"impressions": "1000", "clicks": "50",
                    "costMicros": "12340000", "conversions": 3.2},
        "segments": {"date": "2026-08-01"},
    }
    row.update(overrides)
    return row


def test_google_happy_path_normalizes_micros_and_strings():
    rejected = []
    rows = _parse_google({"results": [google_row()]}, rejected)
    assert rejected == []
    assert rows == [("google", "g-101", "Brand Search", date(2026, 8, 1),
                     1000, 50, 12.34, 3.2)]


def test_google_missing_field_is_dead_lettered_not_fatal():
    rejected = []
    bad = google_row()
    del bad["metrics"]["costMicros"]
    rows = _parse_google({"results": [bad, google_row()]}, rejected)
    assert len(rows) == 1  # good row still loads
    assert len(rejected) == 1
    assert "costMicros" in rejected[0]["reason"]


def test_google_clicks_exceeding_impressions_rejected():
    rejected = []
    bad = google_row()
    bad["metrics"]["clicks"] = "5000"
    rows = _parse_google({"results": [bad]}, rejected)
    assert rows == []
    assert rejected[0]["reason"] == "google sanity check failed"


def meta_row(**overrides):
    row = {
        "campaign_id": "m-201", "campaign_name": "Prospecting",
        "impressions": "2000", "clicks": "40", "spend": "55.50",
        "actions": [{"action_type": "purchase", "value": "4.1"},
                    {"action_type": "link_click", "value": "40"}],
        "date_start": "2026-08-01", "date_stop": "2026-08-01",
    }
    row.update(overrides)
    return row


def test_meta_extracts_purchase_from_actions_array():
    rejected = []
    rows = _parse_meta({"data": [meta_row()]}, rejected)
    assert rejected == []
    assert rows == [("meta", "m-201", "Prospecting", date(2026, 8, 1),
                     2000, 40, 55.50, 4.1)]


def test_meta_no_purchase_action_means_zero_conversions():
    rejected = []
    rows = _parse_meta({"data": [meta_row(actions=[])]}, rejected)
    assert rows[0][7] == 0.0


def test_meta_garbage_values_dead_lettered():
    rejected = []
    rows = _parse_meta({"data": [meta_row(spend="not-a-number")]}, rejected)
    assert rows == []
    assert len(rejected) == 1


def test_orders_parse_and_reject():
    rejected = []
    good = {"order_id": "o-1", "order_date": "2026-08-01",
            "utm_source": "google", "utm_campaign": "g-101", "revenue": 42.5}
    bad = {"order_id": "o-2", "order_date": "yesterday-ish",
           "utm_source": "meta", "utm_campaign": "m-201", "revenue": 10}
    rows = _parse_orders({"orders": [good, bad]}, rejected)
    assert rows == [("o-1", date(2026, 8, 1), "google", "g-101", 42.5)]
    assert len(rejected) == 1
