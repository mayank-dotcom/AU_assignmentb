"""
Unit tests for DND propagation, precedence rules, and phone concurrency.
"""
import os
import tempfile
from datetime import datetime

import pytest

from solution.clock import IST
from solution.config import CampaignConfig, LeadRecord, RetryPolicy, SystemConfig, TenantConfig, WindowConfig
from solution.db import (
    get_final_state_records,
    init_db,
    mark_call_accepted,
    record_call_outcome,
    seed_fixtures,
    select_candidates_to_dial,
)


@pytest.fixture
def multi_campaign_setup():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_dnd.db")
    init_db(db_path)

    win = WindowConfig(start="10:00", end="19:00", days=["thu", "fri"])
    reg = WindowConfig(start="09:00", end="21:00", days=None)

    tenants = {
        "tenant_a": TenantConfig(tenant_id="tenant_a", max_concurrent_calls=5),
        "tenant_b": TenantConfig(tenant_id="tenant_b", max_concurrent_calls=5),
    }

    campaigns = {
        "camp_a": CampaignConfig("camp_a", "tenant_a", "leads_a.csv", win, max_attempts=3),
        "camp_b": CampaignConfig("camp_b", "tenant_b", "leads_b.csv", win, max_attempts=3),
    }

    config = SystemConfig(
        sim_start="2026-11-12T10:00:00+05:30",
        timezone="Asia/Kolkata",
        regulatory_window=reg,
        window_grace_seconds=120,
        retry_policy=RetryPolicy(),
        webhook_secret="test_secret",
        tenants=tenants,
        campaigns=campaigns,
        fixtures_dir=temp_dir,
    )

    shared_phone = "+919999999999"
    leads = [
        LeadRecord("camp_a", "lead_a1", shared_phone, "User A1"),
        LeadRecord("camp_b", "lead_b1", shared_phone, "User B1 (shared phone)"),
        LeadRecord("camp_a", "lead_a2", "+918888888888", "User A2"),
    ]

    seed_fixtures(db_path, config, leads)
    return db_path, config, shared_phone


def test_phone_concurrency_exclusion(multi_campaign_setup):
    db_path, config, shared_phone = multi_campaign_setup
    sim_now = datetime(2026, 11, 12, 11, 0, tzinfo=IST)

    # Both lead_a1 and lead_b1 have shared_phone
    candidates = select_candidates_to_dial(db_path, sim_now, config)

    # Exactly one candidate with shared_phone may be dialed!
    shared_cands = [c for c in candidates if c["phone"] == shared_phone]
    assert len(shared_cands) == 1


def test_dnd_propagation_and_precedence(multi_campaign_setup):
    db_path, config, shared_phone = multi_campaign_setup
    sim_now = datetime(2026, 11, 12, 11, 0, tzinfo=IST)

    candidates = select_candidates_to_dial(db_path, sim_now, config)
    cand_a1 = [c for c in candidates if c["lead_id"] == "lead_a1"][0]

    # lead_a1 is dialed and accepts
    mark_call_accepted(db_path, cand_a1["request_id"], "CA_A1")

    # Call ends with DND disposition!
    ended_at = datetime(2026, 11, 12, 11, 5, tzinfo=IST).isoformat()
    record_call_outcome(
        db_path=db_path,
        call_sid="CA_A1",
        request_id=cand_a1["request_id"],
        status="completed",
        disposition="dnd",
        ended_at_str=ended_at,
        callback_at_str=None,
        duration_sec=60,
        config=config,
    )

    # Now verify final states:
    # 1. lead_a1 got terminal 'dnd' disposition -> completed (Precedence R9)
    # 2. lead_b1 in camp_b was pending with same phone -> dnd_blocked
    records = {r["lead_id"]: r for r in get_final_state_records(db_path)}
    assert records["lead_a1"]["final_state"] == "completed"
    assert records["lead_a1"]["last_disposition"] == "dnd"

    assert records["lead_b1"]["final_state"] == "dnd_blocked"
    assert records["lead_b1"]["attempts"] == 0

    # Ensure lead_b1 can NEVER be selected for dial
    next_cands = select_candidates_to_dial(db_path, sim_now, config)
    assert not any(c["lead_id"] == "lead_b1" for c in next_cands)

