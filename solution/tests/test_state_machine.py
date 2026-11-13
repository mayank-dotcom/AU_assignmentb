"""
Unit tests for the SQLite-backed Lead State Machine.
"""
import os
import tempfile
from datetime import datetime

import pytest

from solution.clock import IST
from solution.config import CampaignConfig, LeadRecord, RetryPolicy, SystemConfig, TenantConfig, WindowConfig
from solution.db import (
    get_final_state_records,
    get_lead_status_summary,
    init_db,
    is_workflow_complete,
    mark_call_accepted,
    record_call_outcome,
    release_unplaced_call,
    seed_fixtures,
    select_candidates_to_dial,
)


@pytest.fixture
def test_setup():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    init_db(db_path)

    win = WindowConfig(start="10:00", end="19:00", days=["thu", "fri"])
    reg = WindowConfig(start="09:00", end="21:00", days=None)

    tenants = {"t1": TenantConfig(tenant_id="t1", max_concurrent_calls=2)}
    campaigns = {
        "c1": CampaignConfig(
            campaign_id="c1",
            tenant_id="t1",
            leads_file="leads.csv",
            window=win,
            max_attempts=3,
        )
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

    leads = [
        LeadRecord(campaign_id="c1", lead_id="l1", phone="+911111111111", customer_name="Alice"),
        LeadRecord(campaign_id="c1", lead_id="l2", phone="+912222222222", customer_name="Bob"),
        LeadRecord(campaign_id="c1", lead_id="l3", phone="+913333333333", customer_name="Charlie"),
    ]

    seed_fixtures(db_path, config, leads)
    return db_path, config


def test_initial_seeding(test_setup):
    db_path, config = test_setup
    summary = get_lead_status_summary(db_path)
    assert summary.get("pending") == 3
    assert not is_workflow_complete(db_path)


def test_concurrency_and_candidate_selection(test_setup):
    db_path, config = test_setup
    sim_now = datetime(2026, 11, 12, 11, 0, tzinfo=IST)

    # Tenant t1 has max concurrency = 2.
    # Out of 3 pending leads, exactly 2 should be selected for dialing.
    candidates = select_candidates_to_dial(db_path, sim_now, config)
    assert len(candidates) == 2

    # Verify leads moved to 'dialing'
    summary = get_lead_status_summary(db_path)
    assert summary.get("dialing") == 2
    assert summary.get("pending") == 1

    # Trying to select again immediately should yield 0 because capacity is full (2/2)
    next_cand = select_candidates_to_dial(db_path, sim_now, config)
    assert len(next_cand) == 0


def test_call_acceptance_and_completion(test_setup):
    db_path, config = test_setup
    sim_now = datetime(2026, 11, 12, 11, 0, tzinfo=IST)

    candidates = select_candidates_to_dial(db_path, sim_now, config)
    cand = candidates[0]
    req_id = cand["request_id"]

    # Provider accepts call -> moves to 'active', attempts incremented to 1
    mark_call_accepted(db_path, req_id, call_sid="CA001")
    summary = get_lead_status_summary(db_path)
    assert summary.get("active") == 1

    # Webhook arrives: interested (terminal disposition)
    ended_at = datetime(2026, 11, 12, 11, 5, tzinfo=IST).isoformat()
    record_call_outcome(
        db_path=db_path,
        call_sid="CA001",
        request_id=req_id,
        status="completed",
        disposition="interested",
        ended_at_str=ended_at,
        callback_at_str=None,
        duration_sec=300,
        config=config,
    )

    # Lead should now be 'completed'
    summary = get_lead_status_summary(db_path)
    assert summary.get("completed") == 1


def test_rejection_503_does_not_count_attempt(test_setup):
    db_path, config = test_setup
    sim_now = datetime(2026, 11, 12, 11, 0, tzinfo=IST)

    candidates = select_candidates_to_dial(db_path, sim_now, config)
    cand = candidates[0]
    req_id = cand["request_id"]

    # Call rejected with 503
    release_unplaced_call(db_path, req_id, sim_delay_seconds=2.0, sim_now=sim_now)

    summary = get_lead_status_summary(db_path)
    # Lead is back in 'pending'
    records = get_final_state_records(db_path)
    l1 = [r for r in records if r["lead_id"] == cand["lead_id"]][0]
    # Attempts must still be 0!
    assert l1["attempts"] == 0


def test_exhausted_after_max_attempts(test_setup):
    db_path, config = test_setup
    sim_now = datetime(2026, 11, 12, 11, 0, tzinfo=IST)

    cand = select_candidates_to_dial(db_path, sim_now, config)[0]
    req1 = cand["request_id"]

    # Attempt 1: busy
    mark_call_accepted(db_path, req1, "CA1")
    ended1 = datetime(2026, 11, 12, 11, 1, tzinfo=IST).isoformat()
    record_call_outcome(db_path, "CA1", req1, "busy", None, ended1, None, 10, config)

    # Advance time by 61 min
    sim_now_2 = datetime(2026, 11, 12, 12, 5, tzinfo=IST)
    cand2 = [c for c in select_candidates_to_dial(db_path, sim_now_2, config) if c["lead_id"] == cand["lead_id"]][0]
    req2 = cand2["request_id"]

    # Attempt 2: no_answer
    mark_call_accepted(db_path, req2, "CA2")
    ended2 = datetime(2026, 11, 12, 12, 6, tzinfo=IST).isoformat()
    record_call_outcome(db_path, "CA2", req2, "no_answer", None, ended2, None, 35, config)

    # Advance time by 61 min
    sim_now_3 = datetime(2026, 11, 12, 13, 10, tzinfo=IST)
    cand3 = [c for c in select_candidates_to_dial(db_path, sim_now_3, config) if c["lead_id"] == cand["lead_id"]][0]
    req3 = cand3["request_id"]

    # Attempt 3 (final allowed attempt): no_answer
    mark_call_accepted(db_path, req3, "CA3")
    ended3 = datetime(2026, 11, 12, 13, 11, tzinfo=IST).isoformat()
    record_call_outcome(db_path, "CA3", req3, "no_answer", None, ended3, None, 35, config)

    records = get_final_state_records(db_path)
    l = [r for r in records if r["lead_id"] == cand["lead_id"]][0]
    assert l["final_state"] == "exhausted"
    assert l["attempts"] == 3
    assert l["last_status"] == "no_answer"

