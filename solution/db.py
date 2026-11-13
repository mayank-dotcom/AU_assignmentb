"""
SQLite database persistence, schema definitions, and transactional state management.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from .config import LeadRecord, SystemConfig
from .scheduler import calculate_next_eligible_time, is_callable_now

IST = timezone(timedelta(hours=5, minutes=30))
TERMINAL_DISPOSITIONS = {"interested", "not_interested", "wrong_number", "dnd"}


@contextmanager
def get_db_connection(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite connection with standard pragma configuration."""
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Initializes tables, views, and indexes."""
    with get_db_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY,
                max_concurrent_calls INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                campaign_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                window_days TEXT NOT NULL,
                max_attempts INTEGER NOT NULL,
                FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
            );

            CREATE TABLE IF NOT EXISTS dnd_numbers (
                phone TEXT PRIMARY KEY,
                discovered_at TEXT NOT NULL,
                source_campaign_id TEXT,
                source_lead_id TEXT
            );

            CREATE TABLE IF NOT EXISTS leads (
                campaign_id TEXT NOT NULL,
                lead_id TEXT NOT NULL,
                phone TEXT NOT NULL,
                customer_name TEXT,
                status TEXT NOT NULL, -- 'pending', 'dialing', 'active', 'completed', 'exhausted', 'dnd_blocked'
                attempts INTEGER NOT NULL DEFAULT 0,
                priority_tier INTEGER NOT NULL DEFAULT 3, -- 1: callback, 2: retry, 3: fresh
                earliest_dial_at TEXT NOT NULL,
                callback_at TEXT,
                current_request_id TEXT,
                current_call_sid TEXT,
                last_status TEXT,
                last_disposition TEXT,
                last_ended_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, lead_id),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
            );

            CREATE INDEX IF NOT EXISTS idx_leads_schedule ON leads (status, priority_tier, earliest_dial_at);
            CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads (phone);
            CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads (campaign_id);

            CREATE TABLE IF NOT EXISTS calls (
                request_id TEXT PRIMARY KEY,
                call_sid TEXT UNIQUE,
                campaign_id TEXT NOT NULL,
                lead_id TEXT NOT NULL,
                phone TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL, -- 'dialing', 'active', 'ended', 'failed_to_place'
                call_status TEXT,
                call_disposition TEXT,
                callback_at TEXT,
                started_at TEXT,
                ended_at TEXT,
                duration_sec INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (campaign_id, lead_id) REFERENCES leads(campaign_id, lead_id)
            );

            CREATE INDEX IF NOT EXISTS idx_calls_active ON calls (status);
            CREATE INDEX IF NOT EXISTS idx_calls_phone ON calls (phone);
            CREATE INDEX IF NOT EXISTS idx_calls_sid ON calls (call_sid);

            CREATE TABLE IF NOT EXISTS processed_webhooks (
                delivery_id TEXT PRIMARY KEY,
                call_sid TEXT NOT NULL,
                processed_at TEXT NOT NULL
            );
            """
        )


def seed_fixtures(db_path: str, config: SystemConfig, leads: List[LeadRecord]) -> None:
    """Inserts tenants, campaigns, and initial leads if not already present."""
    with get_db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE;")
        now_iso = datetime.now(timezone.utc).isoformat()

        # Tenants
        for t in config.tenants.values():
            conn.execute(
                """
                INSERT INTO tenants (tenant_id, max_concurrent_calls)
                VALUES (?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET max_concurrent_calls = excluded.max_concurrent_calls;
                """,
                (t.tenant_id, t.max_concurrent_calls),
            )

        # Campaigns
        for c in config.campaigns.values():
            conn.execute(
                """
                INSERT INTO campaigns (campaign_id, tenant_id, window_start, window_end, window_days, max_attempts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    window_days = excluded.window_days,
                    max_attempts = excluded.max_attempts;
                """,
                (
                    c.campaign_id,
                    c.tenant_id,
                    c.window.start,
                    c.window.end,
                    json.dumps(c.window.days or []),
                    c.max_attempts,
                ),
            )

        # Leads (only insert if not already present, preserving existing state upon restart)
        sim_start_iso = config.sim_start
        for lead in leads:
            conn.execute(
                """
                INSERT OR IGNORE INTO leads (
                    campaign_id, lead_id, phone, customer_name, status,
                    attempts, priority_tier, earliest_dial_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, 3, ?, ?, ?);
                """,
                (
                    lead.campaign_id,
                    lead.lead_id,
                    lead.phone,
                    lead.customer_name,
                    sim_start_iso,
                    now_iso,
                    now_iso,
                ),
            )

        conn.execute("COMMIT;")


def get_active_tenant_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Returns the count of active or dialing calls per tenant."""
    rows = conn.execute(
        """
        SELECT c.tenant_id, COUNT(l.lead_id) AS active_count
        FROM leads l
        JOIN campaigns c ON l.campaign_id = c.campaign_id
        WHERE l.status IN ('dialing', 'active')
        GROUP BY c.tenant_id;
        """
    ).fetchall()
    return {row["tenant_id"]: row["active_count"] for row in rows}


def get_active_phones(conn: sqlite3.Connection) -> Set[str]:
    """Returns the set of phones with an active or dialing call."""
    rows = conn.execute(
        """
        SELECT DISTINCT phone
        FROM leads
        WHERE status IN ('dialing', 'active');
        """
    ).fetchall()
    return {row["phone"] for row in rows}


def get_dnd_phones(conn: sqlite3.Connection) -> Set[str]:
    """Returns the set of all phone numbers in the DND registry."""
    rows = conn.execute("SELECT phone FROM dnd_numbers;").fetchall()
    return {row["phone"] for row in rows}


def select_candidates_to_dial(
    db_path: str,
    sim_now: datetime,
    config: SystemConfig,
    buffer_seconds: float = 45.0,
) -> List[Dict[str, Any]]:
    """
    Selects leads eligible to be dialed right now while respecting:
    - Calling window (campaign + regulatory)
    - Tenant concurrency
    - Phone uniqueness
    - DND rules
    - Priority ordering (callbacks > retries > fresh, lateness order)
    """
    candidates = []
    sim_now_iso = sim_now.astimezone(IST).isoformat()

    with get_db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE;")
        active_tenants = get_active_tenant_counts(conn)
        active_phones = get_active_phones(conn)
        dnd_phones = get_dnd_phones(conn)

        # Check tenant capacity headroom
        tenant_capacity = {}
        for tid, tenant in config.tenants.items():
            used = active_tenants.get(tid, 0)
            tenant_capacity[tid] = max(0, tenant.max_concurrent_calls - used)

        # First, mark any pending lead whose phone is in DND as dnd_blocked
        conn.execute(
            """
            UPDATE leads
            SET status = 'dnd_blocked', updated_at = ?
            WHERE status = 'pending' AND phone IN (SELECT phone FROM dnd_numbers);
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )

        # Query pending leads ordered by priority tier ASC, earliest_dial_at ASC, lead_id ASC
        rows = conn.execute(
            """
            SELECT l.campaign_id, l.lead_id, l.phone, l.attempts, l.priority_tier,
                   l.earliest_dial_at, c.tenant_id, c.max_attempts
            FROM leads l
            JOIN campaigns c ON l.campaign_id = c.campaign_id
            WHERE l.status = 'pending'
              AND l.earliest_dial_at <= ?
            ORDER BY l.priority_tier ASC, l.earliest_dial_at ASC, l.lead_id ASC;
            """,
            (sim_now_iso,),
        ).fetchall()

        reserved_phones: Set[str] = set()

        for row in rows:
            cid = row["campaign_id"]
            lid = row["lead_id"]
            phone = row["phone"]
            tid = row["tenant_id"]
            attempts = row["attempts"]
            max_attempts = row["max_attempts"]

            if attempts >= max_attempts:
                # Mark exhausted
                conn.execute(
                    "UPDATE leads SET status = 'exhausted', updated_at = ? WHERE campaign_id = ? AND lead_id = ?;",
                    (datetime.now(timezone.utc).isoformat(), cid, lid),
                )
                continue

            if phone in dnd_phones:
                conn.execute(
                    "UPDATE leads SET status = 'dnd_blocked', updated_at = ? WHERE campaign_id = ? AND lead_id = ?;",
                    (datetime.now(timezone.utc).isoformat(), cid, lid),
                )
                continue

            if tenant_capacity.get(tid, 0) <= 0:
                continue

            if phone in active_phones or phone in reserved_phones:
                continue

            camp_cfg = config.campaigns.get(cid)
            if not camp_cfg:
                continue

            # Verify window open with safety buffer
            if not is_callable_now(sim_now, camp_cfg.window, config.regulatory_window, buffer_seconds=buffer_seconds):
                continue

            # Reserve this slot
            tenant_capacity[tid] -= 1
            reserved_phones.add(phone)

            # Generate request_id: deterministic per campaign, lead, and attempt number
            attempt_no = attempts + 1
            req_id = f"req_{cid}_{lid}_{attempt_no}"

            now_utc = datetime.now(timezone.utc).isoformat()
            # Atomically mark dialing
            conn.execute(
                """
                UPDATE leads
                SET status = 'dialing', current_request_id = ?, updated_at = ?
                WHERE campaign_id = ? AND lead_id = ?;
                """,
                (req_id, now_utc, cid, lid),
            )

            conn.execute(
                """
                INSERT INTO calls (
                    request_id, campaign_id, lead_id, phone, tenant_id,
                    attempt_no, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'dialing', ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    status = 'dialing', updated_at = excluded.updated_at;
                """,
                (req_id, cid, lid, phone, tid, attempt_no, now_utc, now_utc),
            )

            candidates.append(
                {
                    "campaign_id": cid,
                    "lead_id": lid,
                    "phone": phone,
                    "tenant_id": tid,
                    "attempt_no": attempt_no,
                    "request_id": req_id,
                }
            )

        conn.execute("COMMIT;")

    return candidates


def mark_call_accepted(
    db_path: str,
    request_id: str,
    call_sid: str,
    started_at: Optional[str] = None,
) -> None:
    """Updates call and lead to 'active' once provider returns 201/200."""
    now_utc = datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE;")
        call = conn.execute(
            "SELECT * FROM calls WHERE request_id = ?;", (request_id,)
        ).fetchone()

        if not call:
            conn.execute("COMMIT;")
            return

        cid = call["campaign_id"]
        lid = call["lead_id"]
        attempt_no = call["attempt_no"]

        # Only update status if it has not already been moved to ended by webhook
        if call["status"] != "ended":
            conn.execute(
                """
                UPDATE calls
                SET call_sid = ?, status = 'active', started_at = coalesce(?, started_at), updated_at = ?
                WHERE request_id = ?;
                """,
                (call_sid, started_at, now_utc, request_id),
            )

        # Update lead
        lead = conn.execute(
            "SELECT * FROM leads WHERE campaign_id = ? AND lead_id = ?;", (cid, lid)
        ).fetchone()

        if lead and lead["status"] not in ("completed", "exhausted", "dnd_blocked"):
            # Set attempts to max(current attempts, attempt_no)
            new_attempts = max(lead["attempts"], attempt_no)
            if lead["status"] != "completed":
                new_status = "active" if call["status"] != "ended" else lead["status"]
                conn.execute(
                    """
                    UPDATE leads
                    SET status = ?, attempts = ?, current_call_sid = ?, updated_at = ?
                    WHERE campaign_id = ? AND lead_id = ?;
                    """,
                    (new_status, new_attempts, call_sid, now_utc, cid, lid),
                )

        conn.execute("COMMIT;")


def release_unplaced_call(
    db_path: str,
    request_id: str,
    sim_delay_seconds: float,
    sim_now: datetime,
) -> None:
    """Resets lead back to 'pending' when call was rejected with 503 or confirmed 404."""
    now_utc = datetime.now(timezone.utc).isoformat()
    earliest_dial = (sim_now.astimezone(IST) + timedelta(seconds=sim_delay_seconds)).isoformat()

    with get_db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE;")
        call = conn.execute(
            "SELECT * FROM calls WHERE request_id = ?;", (request_id,)
        ).fetchone()
        if call:
            cid = call["campaign_id"]
            lid = call["lead_id"]
            conn.execute(
                "UPDATE calls SET status = 'failed_to_place', updated_at = ? WHERE request_id = ?;",
                (now_utc, request_id),
            )
            conn.execute(
                """
                UPDATE leads
                SET status = 'pending', current_request_id = NULL,
                    earliest_dial_at = ?, updated_at = ?
                WHERE campaign_id = ? AND lead_id = ? AND status = 'dialing';
                """,
                (earliest_dial, now_utc, cid, lid),
            )
        conn.execute("COMMIT;")


def record_call_outcome(
    db_path: str,
    call_sid: str,
    request_id: Optional[str],
    status: str,
    disposition: Optional[str],
    ended_at_str: str,
    callback_at_str: Optional[str],
    duration_sec: int,
    config: SystemConfig,
) -> None:
    """
    Processes a call completion result (from webhook or polling reconciler).
    Enforces terminal states, DND propagation, retries, and callback scheduling.
    """
    now_utc = datetime.now(timezone.utc).isoformat()
    ended_at = datetime.fromisoformat(ended_at_str).astimezone(IST)
    callback_at = datetime.fromisoformat(callback_at_str).astimezone(IST) if callback_at_str else None

    with get_db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE;")

        # Find call record by call_sid or request_id
        call = None
        if call_sid:
            call = conn.execute(
                "SELECT * FROM calls WHERE call_sid = ?;", (call_sid,)
            ).fetchone()
        if not call and request_id:
            call = conn.execute(
                "SELECT * FROM calls WHERE request_id = ?;", (request_id,)
            ).fetchone()

        if not call:
            # Call record not found yet (e.g. webhook arrived before local db insert committed)
            # Find lead by request_id if available
            cid, lid, phone, tid, attempt_no = None, None, None, None, 1
            if request_id and request_id.startswith("req_"):
                parts = request_id.split("_")
                if len(parts) >= 4:
                    # format: req_{cid}_{lid}_{attempt_no}
                    # cid may contain underscores, e.g. c_acme_renewals
                    attempt_no = int(parts[-1])
                    lid = parts[-2]
                    cid = "_".join(parts[1:-2])
                    lead_row = conn.execute(
                        "SELECT phone FROM leads WHERE campaign_id = ? AND lead_id = ?;",
                        (cid, lid),
                    ).fetchone()
                    if lead_row:
                        phone = lead_row["phone"]
                    camp_row = conn.execute(
                        "SELECT tenant_id FROM campaigns WHERE campaign_id = ?;", (cid,)
                    ).fetchone()
                    if camp_row:
                        tid = camp_row["tenant_id"]

            if cid and lid:
                conn.execute(
                    """
                    INSERT INTO calls (
                        request_id, call_sid, campaign_id, lead_id, phone, tenant_id,
                        attempt_no, status, call_status, call_disposition, callback_at,
                        ended_at, duration_sec, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        request_id or call_sid,
                        call_sid,
                        cid,
                        lid,
                        phone or "",
                        tid or "",
                        attempt_no,
                        "ended",
                        status,
                        disposition,
                        callback_at_str,
                        ended_at_str,
                        duration_sec,
                        now_utc,
                        now_utc,
                    ),
                )
        else:
            cid = call["campaign_id"]
            lid = call["lead_id"]
            phone = call["phone"]
            attempt_no = call["attempt_no"]
            conn.execute(
                """
                UPDATE calls
                SET call_sid = coalesce(?, call_sid),
                    status = 'ended',
                    call_status = ?,
                    call_disposition = ?,
                    callback_at = ?,
                    ended_at = ?,
                    duration_sec = ?,
                    updated_at = ?
                WHERE request_id = ?;
                """,
                (
                    call_sid,
                    status,
                    disposition,
                    callback_at_str,
                    ended_at_str,
                    duration_sec,
                    now_utc,
                    call["request_id"],
                ),
            )

        # Update lead state
        lead = conn.execute(
            "SELECT * FROM leads WHERE campaign_id = ? AND lead_id = ?;", (cid, lid)
        ).fetchone()

        if lead:
            camp_cfg = config.campaigns[cid]
            current_attempts = max(lead["attempts"], attempt_no)

            # Check terminal disposition
            if disposition in TERMINAL_DISPOSITIONS:
                conn.execute(
                    """
                    UPDATE leads
                    SET status = 'completed',
                        attempts = ?,
                        last_status = ?,
                        last_disposition = ?,
                        last_ended_at = ?,
                        current_request_id = NULL,
                        current_call_sid = NULL,
                        updated_at = ?
                    WHERE campaign_id = ? AND lead_id = ?;
                    """,
                    (current_attempts, status, disposition, ended_at_str, now_utc, cid, lid),
                )

                if disposition == "dnd":
                    # Register in global DND table
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO dnd_numbers (phone, discovered_at, source_campaign_id, source_lead_id)
                        VALUES (?, ?, ?, ?);
                        """,
                        (phone, ended_at_str, cid, lid),
                    )
                    # Block all other pending leads with this phone number
                    conn.execute(
                        """
                        UPDATE leads
                        SET status = 'dnd_blocked', updated_at = ?
                        WHERE phone = ? AND status = 'pending';
                        """,
                        (now_utc, phone),
                    )

            elif current_attempts >= camp_cfg.max_attempts:
                # Reached max attempts without terminal disposition -> exhausted
                conn.execute(
                    """
                    UPDATE leads
                    SET status = 'exhausted',
                        attempts = ?,
                        last_status = ?,
                        last_disposition = ?,
                        last_ended_at = ?,
                        current_request_id = NULL,
                        current_call_sid = NULL,
                        updated_at = ?
                    WHERE campaign_id = ? AND lead_id = ?;
                    """,
                    (current_attempts, status, disposition, ended_at_str, now_utc, cid, lid),
                )

            elif disposition == "callback_requested":
                # Callback requested: priority 1
                next_dial = calculate_next_eligible_time(
                    last_status=status,
                    last_disposition=disposition,
                    ended_at=ended_at,
                    callback_at=callback_at,
                    retry_policy=config.retry_policy,
                    camp_window=camp_cfg.window,
                    reg_window=config.regulatory_window,
                )
                conn.execute(
                    """
                    UPDATE leads
                    SET status = 'pending',
                        attempts = ?,
                        priority_tier = 1,
                        earliest_dial_at = ?,
                        callback_at = ?,
                        last_status = ?,
                        last_disposition = ?,
                        last_ended_at = ?,
                        current_request_id = NULL,
                        current_call_sid = NULL,
                        updated_at = ?
                    WHERE campaign_id = ? AND lead_id = ?;
                    """,
                    (
                        current_attempts,
                        next_dial.isoformat(),
                        callback_at_str,
                        status,
                        disposition,
                        ended_at_str,
                        now_utc,
                        cid,
                        lid,
                    ),
                )

            else:
                # Retry pending (no_answer, busy, failed): priority 2
                next_dial = calculate_next_eligible_time(
                    last_status=status,
                    last_disposition=disposition,
                    ended_at=ended_at,
                    callback_at=None,
                    retry_policy=config.retry_policy,
                    camp_window=camp_cfg.window,
                    reg_window=config.regulatory_window,
                )
                conn.execute(
                    """
                    UPDATE leads
                    SET status = 'pending',
                        attempts = ?,
                        priority_tier = 2,
                        earliest_dial_at = ?,
                        last_status = ?,
                        last_disposition = ?,
                        last_ended_at = ?,
                        current_request_id = NULL,
                        current_call_sid = NULL,
                        updated_at = ?
                    WHERE campaign_id = ? AND lead_id = ?;
                    """,
                    (
                        current_attempts,
                        next_dial.isoformat(),
                        status,
                        disposition,
                        ended_at_str,
                        now_utc,
                        cid,
                        lid,
                    ),
                )

        conn.execute("COMMIT;")


def get_active_calls_to_reconcile(db_path: str) -> List[Dict[str, Any]]:
    """Returns all calls currently marked 'active' or 'dialing' for status reconciliation."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT request_id, call_sid, campaign_id, lead_id, phone, status, created_at
            FROM calls
            WHERE status IN ('active', 'dialing');
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_dialing_leads(db_path: str) -> List[Dict[str, Any]]:
    """Returns leads in 'dialing' state for crash recovery."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT campaign_id, lead_id, phone, current_request_id, attempts
            FROM leads
            WHERE status = 'dialing';
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_lead_status_summary(db_path: str) -> Dict[str, int]:
    """Returns counts of leads by their current status."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM leads
            GROUP BY status;
            """
        ).fetchall()
        return {r["status"]: r["count"] for r in rows}


def is_workflow_complete(db_path: str) -> bool:
    """
    Returns True when all leads have reached a terminal state
    ('completed', 'exhausted', 'dnd_blocked') and no calls are active/dialing.
    """
    with get_db_connection(db_path) as conn:
        active_or_pending = conn.execute(
            """
            SELECT COUNT(*) AS remaining
            FROM leads
            WHERE status IN ('pending', 'dialing', 'active');
            """
        ).fetchone()["remaining"]
        return active_or_pending == 0


def get_final_state_records(db_path: str) -> List[Dict[str, Any]]:
    """
    Returns final state records matching the contract, sorted by campaign_id, lead_id.
    Strictly follows precedence: completed > exhausted > dnd_blocked.
    """
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT l.campaign_id, l.lead_id, l.phone, l.status, l.attempts,
                   l.last_status, l.last_disposition,
                   (SELECT COUNT(*) FROM dnd_numbers d WHERE d.phone = l.phone) AS is_dnd,
                   c.max_attempts
            FROM leads l
            JOIN campaigns c ON l.campaign_id = c.campaign_id
            ORDER BY l.campaign_id ASC, l.lead_id ASC;
            """
        ).fetchall()

        results = []
        for r in rows:
            cid = r["campaign_id"]
            lid = r["lead_id"]
            raw_status = r["status"]
            attempts = r["attempts"]
            last_status = r["last_status"] or ""
            last_disp = r["last_disposition"] or ""
            is_dnd = r["is_dnd"] > 0
            max_attempts = r["max_attempts"]

            # Precedence: completed > exhausted > dnd_blocked
            if raw_status == "completed" or last_disp in TERMINAL_DISPOSITIONS:
                final_state = "completed"
            elif attempts >= max_attempts:
                final_state = "exhausted"
            elif is_dnd or raw_status == "dnd_blocked":
                final_state = "dnd_blocked"
            else:
                final_state = raw_status

            results.append(
                {
                    "campaign_id": cid,
                    "lead_id": lid,
                    "final_state": final_state,
                    "attempts": attempts,
                    "last_status": last_status,
                    "last_disposition": last_disp,
                }
            )

        return results
