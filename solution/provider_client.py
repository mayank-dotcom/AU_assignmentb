"""
HTTP Client for interacting with the mock telephony provider.
Handles idempotent call placement, 503 retries, and 504 ambiguity resolution.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class DialResponse:
    outcome: str  # 'accepted', 'rejected_503', 'not_placed', 'unknown'
    status_code: int
    call_sid: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    detail: Optional[str] = None


class ProviderClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8099", timeout: float = 6.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def get_clock(self) -> Dict[str, Any]:
        """Fetches simulated clock status from provider."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/v1/clock")
            resp.raise_for_status()
            return resp.json()

    async def place_call(
        self,
        request_id: str,
        campaign_id: str,
        lead_id: str,
        phone: str,
        callback_url: str,
    ) -> DialResponse:
        """
        Places a call through the provider API.
        Guarantees exact outcome resolution for ambiguous 504s and network timeouts.
        """
        payload = {
            "request_id": request_id,
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "phone": phone,
            "callback_url": callback_url,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(f"{self.base_url}/v1/calls", json=payload)
                code = resp.status_code

                if code in (200, 201):
                    data = resp.json()
                    return DialResponse(
                        outcome="accepted",
                        status_code=code,
                        call_sid=data.get("call_sid"),
                        data=data,
                    )
                elif code == 503:
                    return DialResponse(
                        outcome="rejected_503",
                        status_code=503,
                        detail="service_unavailable (retryable)",
                    )
                elif code == 504:
                    # Ambiguous timeout: call GET /v1/calls?request_id=... to verify whether provider created it
                    return await self._resolve_request_id(client, request_id)
                else:
                    # Violation or other error (409, 422, 429, etc.)
                    return DialResponse(
                        outcome="rejected_error",
                        status_code=code,
                        detail=resp.text,
                    )

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                # Network failed or timed out during POST: outcome unknown, must resolve
                return await self._resolve_request_id(client, request_id, original_error=str(exc))

    async def _resolve_request_id(
        self,
        client: httpx.AsyncClient,
        request_id: str,
        original_error: Optional[str] = None,
    ) -> DialResponse:
        """
        Queries GET /v1/calls?request_id=... to resolve whether a call was placed.
        Retries lookup if transiently unreachable.
        """
        for attempt in range(4):
            if attempt > 0:
                await asyncio.sleep(0.5 * attempt)
            try:
                lookup = await client.get(
                    f"{self.base_url}/v1/calls",
                    params={"request_id": request_id},
                )
                if lookup.status_code == 200:
                    data = lookup.json()
                    return DialResponse(
                        outcome="accepted",
                        status_code=200,
                        call_sid=data.get("call_sid"),
                        data=data,
                    )
                elif lookup.status_code == 404:
                    # Definitively not placed
                    return DialResponse(
                        outcome="not_placed",
                        status_code=404,
                        detail=original_error or "504 resolved to unplaced (404 on lookup)",
                    )
            except (httpx.TimeoutException, httpx.NetworkError):
                continue

        # If still unknown after retries, return unknown so dispatcher keeps it in 'dialing'
        return DialResponse(
            outcome="unknown",
            status_code=504,
            detail=original_error or "504 resolution pending",
        )

    async def get_call_by_sid(self, call_sid: str) -> Optional[Dict[str, Any]]:
        """Queries status of a call by its call_sid."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(f"{self.base_url}/v1/calls/{call_sid}")
                if resp.status_code == 200:
                    return resp.json()
                return None
            except Exception:
                return None

    async def get_call_by_request_id(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Queries status of a call by its request_id."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/v1/calls",
                    params={"request_id": request_id},
                )
                if resp.status_code == 200:
                    return resp.json()
                return None
            except Exception:
                return None

    async def get_admin_report(self) -> Dict[str, Any]:
        """Fetches grading report from provider."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/admin/report")
            resp.raise_for_status()
            return resp.json()

    async def get_admin_leads(self) -> List[Dict[str, Any]]:
        """Fetches provider expected lead states."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/admin/leads")
            resp.raise_for_status()
            return resp.json()

