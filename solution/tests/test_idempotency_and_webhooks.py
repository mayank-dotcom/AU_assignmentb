"""
Unit tests for webhook verification, duplicate delivery idempotency, and 504 resolution.
"""
import hashlib
import hmac
import json
import os
import tempfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from solution.clock import IST
from solution.config import CampaignConfig, LeadRecord, RetryPolicy, SystemConfig, TenantConfig, WindowConfig
from solution.db import get_final_state_records, init_db, mark_call_accepted, seed_fixtures, select_candidates_to_dial
from solution.webhook_server import create_webhook_app


@pytest.fixture
def webhook_env():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_webhook.db")
    init_db(db_path)

    win = WindowConfig(start="10:00", end="19:00", days=["thu", "fri"])
    reg = WindowConfig(start="09:00", end="21:00", days=None)
    tenants = {"t1": TenantConfig("t1", 5)}
    campaigns = {"c1": CampaignConfig("c1", "t1", "leads.csv", win, 3)}

    secret = "my_secret_key"
    config = SystemConfig(
        sim_start="2026-11-12T10:00:00+05:30",
        timezone="Asia/Kolkata",
        regulatory_window=reg,
        window_grace_seconds=120,
        retry_policy=RetryPolicy(),
        webhook_secret=secret,
        tenants=tenants,
        campaigns=campaigns,
        fixtures_dir=temp_dir,
    )

    leads = [LeadRecord("c1", "lead1", "+919876543210", "Test User")]
    seed_fixtures(db_path, config, leads)

    app = create_webhook_app(db_path, config)
    client = TestClient(app)
    return db_path, config, client, secret


def test_webhook_signature_verification(webhook_env):
    db_path, config, client, secret = webhook_env

    payload = {
        "event": "call.completed",
        "call_sid": "CA12345",
        "request_id": "req_c1_lead1_1",
        "tenant_id": "t1",
        "campaign_id": "c1",
        "lead_id": "lead1",
        "phone": "+919876543210",
        "status": "completed",
        "disposition": "interested",
        "started_at": "2026-11-12T11:00:00+05:30",
        "ended_at": "2026-11-12T11:05:00+05:30",
        "duration_sec": 300,
    }
    body_bytes = json.dumps(payload).encode()
    valid_sig = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()

    # 1. Missing signature
    resp = client.post("/webhooks/telephony", content=body_bytes)
    assert resp.status_code == 401

    # 2. Invalid signature
    resp = client.post("/webhooks/telephony", content=body_bytes, headers={"X-AU-Signature": "invalid_sig"})
    assert resp.status_code == 401

    # 3. Valid signature
    resp = client.post(
        "/webhooks/telephony",
        content=body_bytes,
        headers={"X-AU-Signature": valid_sig, "X-AU-Delivery-Id": "deliv-001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    # 4. Duplicate webhook delivery
    resp_dup = client.post(
        "/webhooks/telephony",
        content=body_bytes,
        headers={"X-AU-Signature": valid_sig, "X-AU-Delivery-Id": "deliv-001"},
    )
    assert resp_dup.status_code == 200
    assert resp_dup.json()["status"] == "duplicate_ignored"


def test_webhook_arriving_before_call_accepted(webhook_env):
    db_path, config, client, secret = webhook_env
    sim_now = datetime(2026, 11, 12, 11, 0, tzinfo=IST)

    # Lead selected for dial
    candidates = select_candidates_to_dial(db_path, sim_now, config)
    cand = candidates[0]
    req_id = cand["request_id"]

    # Webhook arrives BEFORE client gets response
    payload = {
        "event": "call.completed",
        "call_sid": "CA_FAST",
        "request_id": req_id,
        "tenant_id": "t1",
        "campaign_id": "c1",
        "lead_id": "lead1",
        "phone": "+919876543210",
        "status": "completed",
        "disposition": "interested",
        "ended_at": "2026-11-12T11:01:00+05:30",
        "duration_sec": 10,
    }
    body_bytes = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()

    resp = client.post(
        "/webhooks/telephony",
        content=body_bytes,
        headers={"X-AU-Signature": sig, "X-AU-Delivery-Id": "deliv-fast"},
    )
    assert resp.status_code == 200

    # Now the client's HTTP call finishes and attempts to mark accepted
    mark_call_accepted(db_path, req_id, "CA_FAST")

    # Lead must stay in completed terminal state
    records = {r["lead_id"]: r for r in get_final_state_records(db_path)}
    assert records["lead1"]["final_state"] == "completed"
    assert records["lead1"]["last_disposition"] == "interested"

