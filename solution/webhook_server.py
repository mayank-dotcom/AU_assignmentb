"""
FastAPI webhook receiver service for call completion events.
Verifies HMAC-SHA256 signatures and handles duplicate/out-of-order deliveries idempotently.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .config import SystemConfig
from .db import (
    get_db_connection,
    get_lead_status_summary,
    record_call_outcome,
)

logger = logging.getLogger("webhook_receiver")


def create_webhook_app(db_path: str, config: SystemConfig) -> FastAPI:
    app = FastAPI(title="Agentic Universe Telephony Webhook Receiver", version="1.0")

    @app.post("/webhooks/telephony")
    async def receive_webhook(
        request: Request,
        x_au_signature: Optional[str] = Header(None, alias="X-AU-Signature"),
        x_au_delivery_id: Optional[str] = Header(None, alias="X-AU-Delivery-Id"),
    ):
        raw_body = await request.body()

        # 1. Signature Verification
        if not x_au_signature:
            logger.warning("Missing X-AU-Signature header")
            raise HTTPException(status_code=401, detail="missing_signature")

        expected_sig = hmac.new(
            config.webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_sig, x_au_signature):
            logger.warning("Invalid webhook signature")
            raise HTTPException(status_code=401, detail="invalid_signature")

        # 2. Parse payload
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_json")

        call_sid = payload.get("call_sid")
        delivery_id = x_au_delivery_id or f"{call_sid}_{datetime.now(timezone.utc).timestamp()}"

        # 3. Deduplication check via delivery_id
        with get_db_connection(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            existing = conn.execute(
                "SELECT delivery_id FROM processed_webhooks WHERE delivery_id = ?;",
                (delivery_id,),
            ).fetchone()
            if existing:
                conn.execute("COMMIT;")
                # Already processed this exact webhook delivery
                return JSONResponse({"status": "duplicate_ignored"}, status_code=200)

            conn.execute(
                "INSERT INTO processed_webhooks (delivery_id, call_sid, processed_at) VALUES (?, ?, ?);",
                (delivery_id, call_sid or "", datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("COMMIT;")

        # 4. Record call outcome in DB state machine
        record_call_outcome(
            db_path=db_path,
            call_sid=payload.get("call_sid"),
            request_id=payload.get("request_id"),
            status=payload.get("status"),
            disposition=payload.get("disposition"),
            ended_at_str=payload.get("ended_at") or datetime.now(timezone.utc).isoformat(),
            callback_at_str=payload.get("callback_at"),
            duration_sec=int(payload.get("duration_sec", 0)),
            config=config,
        )

        return JSONResponse({"status": "accepted", "call_sid": call_sid}, status_code=200)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/status")
    async def status():
        summary = get_lead_status_summary(db_path)
        return {"lead_summary": summary}

    return app

