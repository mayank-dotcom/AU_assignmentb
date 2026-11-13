"""
CSV export utility for final_state.csv according to the output contract.
"""
from __future__ import annotations

import csv
from typing import List

from .db import get_final_state_records


def export_final_state_csv(db_path: str, output_path: str) -> List[dict]:
    """
    Exports final lead states to CSV sorted by campaign_id, lead_id.
    Returns the written rows for verification.
    """
    records = get_final_state_records(db_path)

    fieldnames = [
        "campaign_id",
        "lead_id",
        "final_state",
        "attempts",
        "last_status",
        "last_disposition",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for r in records:
            writer.writerow(
                {
                    "campaign_id": r["campaign_id"],
                    "lead_id": r["lead_id"],
                    "final_state": r["final_state"],
                    "attempts": r["attempts"],
                    "last_status": r["last_status"] or "",
                    "last_disposition": r["last_disposition"] or "",
                }
            )

    return records

