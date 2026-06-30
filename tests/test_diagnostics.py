"""Unit tests for the judgment layer — no database, just canned provider output.

This is the payoff of keeping diagnostics.py pure: the reasoning is testable
against fixed inputs.
"""

from __future__ import annotations

from pg_logical_mcp import diagnostics

MB = 1024 * 1024


# -- assess_slot_risk -----------------------------------------------------


def test_lost_slot_is_critical():
    slots = [{"slot_name": "s1", "wal_status": "lost", "pinned_wal_bytes": 100 * MB, "active": False}]
    result = diagnostics.assess_slot_risk(slots, wal_bytes_per_second=1 * MB, max_slot_wal_keep_bytes=64 * MB)
    assert result["severity"] == "critical"
    assert "invalidated" in result["summary"]


def test_slot_near_invalidation_projects_time():
    # 60MB pinned, 64MB cap => 4MB headroom; at 1MB/s that's ~4s to invalidation.
    slots = [{"slot_name": "s1", "wal_status": "extended", "pinned_wal_bytes": 60 * MB, "active": True}]
    result = diagnostics.assess_slot_risk(slots, wal_bytes_per_second=1 * MB, max_slot_wal_keep_bytes=64 * MB)
    assert result["severity"] == "critical"
    assert result["closest_slot"]["slot_name"] == "s1"
    assert 3 <= result["closest_slot"]["time_to_invalidation_seconds"] <= 5


def test_unlimited_keep_size_warns_about_disk():
    slots = [{"slot_name": "s1", "wal_status": "reserved", "pinned_wal_bytes": 10 * MB, "active": False}]
    result = diagnostics.assess_slot_risk(slots, wal_bytes_per_second=1 * MB, max_slot_wal_keep_bytes=None)
    assert result["severity"] == "info"
    assert result["closest_slot"] is None


def test_no_risk_is_ok():
    slots = [{"slot_name": "s1", "wal_status": "reserved", "pinned_wal_bytes": 1 * MB, "active": True}]
    result = diagnostics.assess_slot_risk(slots, wal_bytes_per_second=1024, max_slot_wal_keep_bytes=10_000 * MB)
    assert result["severity"] == "ok"


def test_disk_fill_projection_when_available_bytes_given():
    slots = [{"slot_name": "s1", "wal_status": "reserved", "pinned_wal_bytes": 1 * MB, "active": True}]
    result = diagnostics.assess_slot_risk(
        slots, wal_bytes_per_second=1 * MB, max_slot_wal_keep_bytes=None, available_disk_bytes=10 * MB
    )
    assert result["time_to_disk_fill_seconds"] == 10.0


# -- check_publication_coverage ------------------------------------------


def test_expected_table_missing_from_publication():
    published = [{"schemaname": "public", "tablename": "orders"}]
    identities = [
        {"schemaname": "public", "tablename": "orders", "replica_identity": "default", "has_primary_key": True}
    ]
    result = diagnostics.check_publication_coverage(published, identities, ["public.orders", "public.audit_log"])
    assert result["severity"] == "warning"
    assert "public.audit_log" in result["missing_from_publication"]


def test_published_table_without_usable_replica_identity():
    published = [{"schemaname": "public", "tablename": "events"}]
    identities = [
        {"schemaname": "public", "tablename": "events", "replica_identity": "default", "has_primary_key": False}
    ]
    result = diagnostics.check_publication_coverage(published, identities, ["public.events"])
    assert result["severity"] == "warning"
    assert result["replica_identity_problems"][0]["tablename"] == "events"


def test_full_coverage_is_ok():
    published = [{"schemaname": "public", "tablename": "orders"}]
    identities = [
        {"schemaname": "public", "tablename": "orders", "replica_identity": "default", "has_primary_key": True}
    ]
    result = diagnostics.check_publication_coverage(published, identities, ["orders"])
    assert result["severity"] == "ok"


# -- diagnose_stuck_subscription -----------------------------------------


def test_stuck_subscription_identifies_wedge_and_options():
    subs = [
        {"subname": "app_sub", "subenabled": True, "pid": 123, "received_lsn": "0/16B3748", "latest_end_lsn": "0/16B3748"}
    ]
    errors = [{"subname": "app_sub", "apply_error_count": 3, "sync_error_count": 0}]
    result = diagnostics.diagnose_stuck_subscription(subs, errors)
    assert result["severity"] == "critical"
    finding = result["subscriptions"][0]
    assert finding["stuck"] is True
    assert finding["wedged_at_lsn"] == "0/16B3748"
    options = {o["option"] for o in finding["options"]}
    assert options == {"resolve_the_row", "skip_the_transaction"}


def test_healthy_subscription_not_flagged():
    subs = [{"subname": "app_sub", "subenabled": True, "pid": 123, "received_lsn": "0/16B3748"}]
    errors = [{"subname": "app_sub", "apply_error_count": 0, "sync_error_count": 0}]
    result = diagnostics.diagnose_stuck_subscription(subs, errors)
    assert result["severity"] == "ok"
    assert result["subscriptions"][0]["stuck"] is False


def test_enabled_subscription_with_no_worker_is_stuck():
    subs = [{"subname": "app_sub", "subenabled": True, "pid": None, "received_lsn": "0/16B3748"}]
    errors = []
    result = diagnostics.diagnose_stuck_subscription(subs, errors)
    assert result["subscriptions"][0]["stuck"] is True
