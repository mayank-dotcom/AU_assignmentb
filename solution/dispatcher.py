"""
Core Dispatcher Workflow Engine.
Coordinates candidate selection, call placement, reconciliation, and crash recovery.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .clock import Clock, ProviderClock
from .config import SystemConfig
from .db import (
    get_active_calls_to_reconcile,
    get_all_dialing_leads,
    get_db_connection,
    get_lead_status_summary,
    is_workflow_complete,
    mark_call_accepted,
    record_call_outcome,
    release_unplaced_call,
    select_candidates_to_dial,
)
from .export import export_final_state_csv
from .provider_client import ProviderClient
from .scheduler import is_callable_now, next_combined_open

logger = logging.getLogger("dispatcher")
IST = timezone(timedelta(hours=5, minutes=30))


class DispatcherEngine:
    def __init__(
        self,
        db_path: str,
        config: SystemConfig,
        client: ProviderClient,
        callback_url: str,
        output_csv_path: str = "final_state.csv",
        clock: Optional[Clock] = None,
    ):
        self.db_path = db_path
        self.config = config
        self.client = client
        self.callback_url = callback_url
        self.output_csv_path = output_csv_path
        self.clock = clock
        self.is_running = False
        self._stop_event = asyncio.Event()

    async def init_clock(self) -> Clock:
        """Initializes or syncs simulated clock from provider."""
        clock_data = await self.client.get_clock()
        self.clock = ProviderClock.from_clock_dict(clock_data)
        logger.info(
            f"Clock synchronized: sim_now={self.clock.now().isoformat()} speed={self.clock.get_speed()}x"
        )
        return self.clock

    async def startup_recovery(self) -> None:
        """
        Crash recovery on startup:
        1. Resolves any leads that were mid-'dialing' when crashed.
        2. Reconciles any active calls against provider status.
        """
        logger.info("Executing startup recovery check...")

        # 1. Reconcile dialing leads
        dialing_leads = get_all_dialing_leads(self.db_path)
        if dialing_leads:
            logger.info(f"Found {len(dialing_leads)} leads in 'dialing' state during recovery")
            for lead in dialing_leads:
                req_id = lead["current_request_id"]
                if not req_id:
                    continue
                call_info = await self.client.get_call_by_request_id(req_id)
                now_sim = self.clock.now() if self.clock else datetime.now(IST)
                if call_info:
                    logger.info(f"Recovery: call {req_id} was accepted by provider (call_sid={call_info.get('call_sid')})")
                    mark_call_accepted(
                        db_path=self.db_path,
                        request_id=req_id,
                        call_sid=call_info["call_sid"],
                        started_at=call_info.get("created_at"),
                    )
                    if call_info.get("state") == "ended" and "result" in call_info:
                        res = call_info["result"]
                        record_call_outcome(
                            db_path=self.db_path,
                            call_sid=res["call_sid"],
                            request_id=req_id,
                            status=res["status"],
                            disposition=res.get("disposition"),
                            ended_at_str=res["ended_at"],
                            callback_at_str=res.get("callback_at"),
                            duration_sec=int(res.get("duration_sec", 0)),
                            config=self.config,
                        )
                else:
                    logger.info(f"Recovery: call {req_id} was NOT placed on provider. Releasing lead.")
                    release_unplaced_call(
                        db_path=self.db_path,
                        request_id=req_id,
                        sim_delay_seconds=2.0,
                        sim_now=now_sim,
                    )

        # 2. Reconcile active calls
        await self.reconcile_active_calls()

    async def reconcile_active_calls(self) -> None:
        """
        Polls provider for all active calls to catch lost webhooks or out-of-order completions.
        """
        active_calls = get_active_calls_to_reconcile(self.db_path)
        if not active_calls:
            return

        for call in active_calls:
            call_sid = call.get("call_sid")
            req_id = call.get("request_id")
            info = None
            if call_sid:
                info = await self.client.get_call_by_sid(call_sid)
            elif req_id:
                info = await self.client.get_call_by_request_id(req_id)

            if info and info.get("state") == "ended" and "result" in info:
                res = info["result"]
                record_call_outcome(
                    db_path=self.db_path,
                    call_sid=res["call_sid"],
                    request_id=req_id,
                    status=res["status"],
                    disposition=res.get("disposition"),
                    ended_at_str=res["ended_at"],
                    callback_at_str=res.get("callback_at"),
                    duration_sec=int(res.get("duration_sec", 0)),
                    config=self.config,
                )
            elif call.get("status") == "dialing" and info is None and req_id:
                # Call was dialing locally but provider has no record of it -> release lead
                now_sim = self.clock.now() if self.clock else datetime.now(IST)
                logger.info(f"Reconciler: call {req_id} not found on provider. Releasing lead.")
                release_unplaced_call(self.db_path, req_id, sim_delay_seconds=2.0, sim_now=now_sim)

    async def dispatch_cycle(self) -> int:
        """
        Performs one dispatch cycle:
        1. Selects eligible candidates.
        2. Dials them concurrently via provider API.
        3. Updates DB states based on exact response.
        Returns the number of calls placed.
        """
        if not self.clock:
            await self.init_clock()
        assert self.clock is not None

        sim_now = self.clock.now()
        speed = self.clock.get_speed() if self.clock else 600.0
        # Dynamic safe buffer based on clock speed (at least 2.5 real seconds equivalent in sim time)
        candidate_buffer = max(180.0, speed * 2.5)
        pre_dial_buffer = max(180.0, speed * 2.0)

        candidates = select_candidates_to_dial(self.db_path, sim_now, self.config, buffer_seconds=candidate_buffer)

        if not candidates:
            return 0

        logger.info(f"Dispatching {len(candidates)} eligible calls at sim_time {sim_now.isoformat()}")

        async def _dial_one(cand) -> bool:
            req_id = cand["request_id"]
            now_sim = self.clock.now()
            camp_cfg = self.config.campaigns[cand["campaign_id"]]

            # Verify window is still open immediately before dial
            if not is_callable_now(now_sim, camp_cfg.window, self.config.regulatory_window, buffer_seconds=pre_dial_buffer):
                next_open = next_combined_open(now_sim, camp_cfg.window, self.config.regulatory_window)
                logger.info(
                    f"Window closed or closing for {cand['campaign_id']} before dial. Rescheduling {req_id} to {next_open.isoformat()}"
                )
                release_unplaced_call(
                    self.db_path,
                    req_id,
                    sim_delay_seconds=0.0,
                    sim_now=next_open,
                )
                return False

            resp = await self.client.place_call(
                request_id=req_id,
                campaign_id=cand["campaign_id"],
                lead_id=cand["lead_id"],
                phone=cand["phone"],
                callback_url=self.callback_url,
            )

            if resp.outcome == "accepted":
                call_sid = resp.call_sid or ""
                started_at = resp.data.get("created_at") if resp.data else None
                mark_call_accepted(self.db_path, req_id, call_sid, started_at)
                logger.info(f"Call placed: {req_id} -> {call_sid}")
                return True
            elif resp.outcome in ("rejected_503", "not_placed"):
                logger.info(f"Call unplaced ({resp.outcome}): {req_id}. Resetting lead.")
                release_unplaced_call(self.db_path, req_id, sim_delay_seconds=2.0, sim_now=now_sim)
                return False
            elif resp.outcome == "unknown":
                logger.warning(f"Call outcome unknown for {req_id}; will reconcile in next cycle.")
                return False
            else:
                logger.error(f"Call placement error for {req_id}: {resp.status_code} - {resp.detail}")
                # Release lead so it does not stay stuck in 'dialing'
                release_unplaced_call(self.db_path, req_id, sim_delay_seconds=10.0, sim_now=now_sim)
                return False

        results = await asyncio.gather(*[_dial_one(cand) for cand in candidates])
        return sum(1 for r in results if r)

    async def run(self) -> None:
        """Main dispatcher loop."""
        self.is_running = True
        logger.info("Dispatcher engine started")

        if not self.clock:
            await self.init_clock()

        await self.startup_recovery()

        last_reconcile_time = 0.0

        try:
            while not self._stop_event.is_set():
                now_real = asyncio.get_event_loop().time()

                # Periodic active call reconciliation (every 0.5 real seconds)
                if now_real - last_reconcile_time >= 0.5:
                    await self.reconcile_active_calls()
                    last_reconcile_time = now_real

                # Check if entire workflow is complete
                if is_workflow_complete(self.db_path):
                    # Final reconciliation pass to ensure all calls ended
                    await self.reconcile_active_calls()
                    if is_workflow_complete(self.db_path):
                        logger.info("All leads have reached a final state! Workflow complete.")
                        break

                # Dispatch calls
                placed = await self.dispatch_cycle()

                if placed > 0:
                    # Immediately check if more capacity is available
                    await asyncio.sleep(0.01)
                else:
                    # Determine appropriate sleep duration
                    summary = get_lead_status_summary(self.db_path)
                    active_count = summary.get("active", 0) + summary.get("dialing", 0)

                    if active_count > 0:
                        # Calls are active, sleep briefly for webhooks/progress
                        await asyncio.sleep(0.2)
                    else:
                        # No active calls, waiting for window open or retry/callback time
                        # Query earliest pending dial time
                        earliest_pending = self._get_earliest_pending_time()
                        if earliest_pending and self.clock:
                            sim_now = self.clock.now()
                            diff_sec = (earliest_pending - sim_now).total_seconds()
                            if diff_sec > 0:
                                real_sec = min(1.0, max(0.1, self.clock.sim_to_real_seconds(diff_sec)))
                                await asyncio.sleep(real_sec)
                            else:
                                await asyncio.sleep(0.1)
                        else:
                            await asyncio.sleep(0.2)

        finally:
            self.is_running = False
            # Export final_state.csv
            records = export_final_state_csv(self.db_path, self.output_csv_path)
            logger.info(f"Exported {len(records)} records to {self.output_csv_path}")

    def _get_earliest_pending_time(self) -> Optional[datetime]:
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT MIN(earliest_dial_at) AS earliest FROM leads WHERE status = 'pending';"
            ).fetchone()
            if row and row["earliest"]:
                try:
                    return datetime.fromisoformat(row["earliest"]).astimezone(IST)
                except Exception:
                    return None
            return None

    def stop(self) -> None:
        """Signals the engine to stop."""
        self._stop_event.set()
