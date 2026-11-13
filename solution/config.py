"""
Configuration dataclasses and loader for campaigns and tenants.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class WindowConfig:
    start: str
    end: str
    days: Optional[List[str]] = None


@dataclass
class RetryPolicy:
    no_answer_minutes: int = 60
    busy_minutes: int = 60
    failed_minutes: int = 15


@dataclass
class TenantConfig:
    tenant_id: str
    max_concurrent_calls: int


@dataclass
class CampaignConfig:
    campaign_id: str
    tenant_id: str
    leads_file: str
    window: WindowConfig
    max_attempts: int


@dataclass
class LeadRecord:
    campaign_id: str
    lead_id: str
    phone: str
    customer_name: str


@dataclass
class SystemConfig:
    sim_start: str
    timezone: str
    regulatory_window: WindowConfig
    window_grace_seconds: int
    retry_policy: RetryPolicy
    webhook_secret: str
    tenants: Dict[str, TenantConfig]
    campaigns: Dict[str, CampaignConfig]
    fixtures_dir: str

    @classmethod
    def load(cls, fixtures_path: str) -> SystemConfig:
        fixtures_path = os.path.abspath(fixtures_path)
        fixtures_dir = os.path.dirname(fixtures_path)
        with open(fixtures_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        reg_win = WindowConfig(
            start=data["regulatory_window"]["start"],
            end=data["regulatory_window"]["end"],
            days=None,
        )

        rp_data = data.get("retry_policy", {})
        retry_policy = RetryPolicy(
            no_answer_minutes=rp_data.get("no_answer_minutes", 60),
            busy_minutes=rp_data.get("busy_minutes", 60),
            failed_minutes=rp_data.get("failed_minutes", 15),
        )

        tenants = {}
        for t in data.get("tenants", []):
            tenants[t["tenant_id"]] = TenantConfig(
                tenant_id=t["tenant_id"],
                max_concurrent_calls=int(t["max_concurrent_calls"]),
            )

        campaigns = {}
        for c in data.get("campaigns", []):
            win_data = c["window"]
            window = WindowConfig(
                start=win_data["start"],
                end=win_data["end"],
                days=win_data.get("days"),
            )
            campaigns[c["campaign_id"]] = CampaignConfig(
                campaign_id=c["campaign_id"],
                tenant_id=c["tenant_id"],
                leads_file=c["leads_file"],
                window=window,
                max_attempts=int(c.get("max_attempts", 3)),
            )

        return cls(
            sim_start=data["sim_start"],
            timezone=data.get("timezone", "Asia/Kolkata"),
            regulatory_window=reg_win,
            window_grace_seconds=data.get("window_grace_seconds", 120),
            retry_policy=retry_policy,
            webhook_secret=data.get("webhook_secret", ""),
            tenants=tenants,
            campaigns=campaigns,
            fixtures_dir=fixtures_dir,
        )

    def load_leads(self) -> List[LeadRecord]:
        """Loads all leads from CSV files defined in campaigns."""
        leads = []
        for camp in self.campaigns.values():
            file_path = os.path.join(self.fixtures_dir, camp.leads_file)
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Leads file not found: {file_path}")
            with open(file_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    leads.append(
                        LeadRecord(
                            campaign_id=camp.campaign_id,
                            lead_id=row["lead_id"].strip(),
                            phone=row["phone"].strip(),
                            customer_name=row.get("customer_name", "").strip(),
                        )
                    )
        return leads

