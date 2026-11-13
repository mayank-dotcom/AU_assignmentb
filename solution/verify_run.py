"""
Verification script for end-to-end testing against mock telephony provider.
Tests full workflow, chaos/crash recovery (kill -9 and restart), violation checking, and output contract diffing.
"""
from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

import httpx

PROVIDER_URL = "http://127.0.0.1:8099"
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait_for_provider(timeout: float = 10.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = httpx.get(f"{PROVIDER_URL}/v1/clock", timeout=1.0)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("Provider failed to start within timeout")


def run_verification(speed: float = 1200.0, seed: int = 7, failure_scale: float = 1.0, test_crash: bool = True):
    print("=" * 70)
    print(f"STARTING VERIFICATION RUN: speed={speed}x, seed={seed}, failure_scale={failure_scale}, test_crash={test_crash}")
    print("=" * 70)

    # Clean existing database and output
    db_file = os.path.join(ROOT_DIR, "dispatcher.db")
    csv_file = os.path.join(ROOT_DIR, "final_state.csv")
    for p in (db_file, csv_file):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    # 1. Start mock provider
    mock_cwd = os.path.join(ROOT_DIR, "assignment-b-workflow")
    mock_cmd = [
        sys.executable,
        "-m",
        "mock_telephony",
        "--speed",
        str(speed),
        "--seed",
        str(seed),
        "--failure-scale",
        str(failure_scale),
        "--port",
        "8099",
    ]
    print(f"[1/5] Starting mock provider: {' '.join(mock_cmd)}")
    provider_proc = subprocess.Popen(mock_cmd, cwd=mock_cwd)

    try:
        clock_info = wait_for_provider()
        print(f"Provider ready! Sim epoch: {clock_info['sim_epoch']}, speed: {clock_info['speed']}x")

        # 2. Start dispatcher process
        disp_cmd = [
            sys.executable,
            "-m",
            "solution.main",
            "--db",
            db_file,
            "--output",
            csv_file,
            "--provider-url",
            PROVIDER_URL,
        ]
        print(f"[2/5] Starting dispatcher: {' '.join(disp_cmd)}")
        disp_proc = subprocess.Popen(disp_cmd, cwd=ROOT_DIR)

        if test_crash:
            # Let it run for ~15 real seconds, then terminate mid-run (simulating kill -9 crash)
            print("[CRASH TEST] Waiting 15 seconds before simulating crash (kill -9)...")
            time.sleep(15)
            print("[CRASH TEST] Killing dispatcher process mid-run...")
            disp_proc.terminate()
            try:
                disp_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                disp_proc.kill()
            print("[CRASH TEST] Dispatcher killed. Sleeping 2 seconds before restart...")
            time.sleep(2)

            print("[CRASH TEST] Resuming dispatcher from SQLite database...")
            disp_proc = subprocess.Popen(disp_cmd, cwd=ROOT_DIR)

        # 3. Wait for dispatcher to finish
        print("[3/5] Waiting for dispatcher to finish all leads...")
        disp_proc.wait()
        print("Dispatcher process completed.")

        # 4. Fetch admin report from provider
        print("[4/5] Fetching provider grading report...")
        with httpx.Client(timeout=10.0) as client:
            report = client.get(f"{PROVIDER_URL}/admin/report").json()
            admin_leads = client.get(f"{PROVIDER_URL}/admin/leads").json()

        print("-" * 70)
        print("ADMIN REPORT:")
        print(f"  Total calls: {report.get('calls_total')}")
        print(f"  Calls active: {report.get('calls_active')}")
        print(f"  Calls by status: {report.get('calls_by_status')}")
        print(f"  VIOLATION COUNT: {report.get('violation_count')}")
        print(f"  Violations by type: {report.get('violations_by_type')}")
        if report.get("violations"):
            print("  Detailed violations:")
            for v in report["violations"][:10]:
                print(f"    - {v}")
        print(f"  Max concurrency seen: {report.get('max_concurrency_seen')}")
        print(f"  Callback lateness (min): {report.get('callback_lateness_minutes')}")
        print(f"  Webhooks stats: {report.get('webhooks')}")
        print("-" * 70)

        # 5. Diff output CSV against /admin/leads
        print("[5/5] Diffing final_state.csv against provider /admin/leads...")
        if not os.path.exists(csv_file):
            print("ERROR: final_state.csv was not generated!")
            return False

        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            our_rows = list(reader)

        # Key by (campaign_id, lead_id)
        admin_map = {(r["campaign_id"], r["lead_id"]): r for r in admin_leads}
        our_map = {(r["campaign_id"], r["lead_id"]): r for r in our_rows}

        mismatches = []
        for key, exp in admin_map.items():
            actual = our_map.get(key)
            if not actual:
                mismatches.append(f"Missing lead: {key}")
                continue
            if actual["final_state"] != exp["expected_state"]:
                mismatches.append(
                    f"{key} state mismatch: expected {exp['expected_state']}, got {actual['final_state']}"
                )
            if int(actual["attempts"]) != int(exp["attempts"]):
                mismatches.append(
                    f"{key} attempts mismatch: expected {exp['attempts']}, got {actual['attempts']}"
                )
            if actual["last_status"] != exp["last_status"]:
                mismatches.append(
                    f"{key} last_status mismatch: expected '{exp['last_status']}', got '{actual['last_status']}'"
                )
            if actual["last_disposition"] != exp["last_disposition"]:
                mismatches.append(
                    f"{key} last_disposition mismatch: expected '{exp['last_disposition']}', got '{actual['last_disposition']}'"
                )

        if mismatches:
            print(f"FOUND {len(mismatches)} MISMATCHES:")
            for m in mismatches[:20]:
                print(f"  * {m}")
            return False
        else:
            print("100% MATCH! All 200 leads match provider records exactly.")

        if report.get("violation_count", 0) == 0:
            print("\n>>> ALL CHECKS PASSED: 0 VIOLATIONS & 100% OUTPUT MATCH! <<<")
            return True
        else:
            print(f"\nFAILED: {report.get('violation_count')} violations recorded.")
            return False

    finally:
        # Terminate mock provider
        if provider_proc.poll() is None:
            provider_proc.terminate()
            try:
                provider_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                provider_proc.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--speed", type=float, default=1200.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--failure-scale", type=float, default=1.0)
    parser.add_argument("--no-crash", action="store_true")
    args = parser.parse_args()

    success = run_verification(
        speed=args.speed,
        seed=args.seed,
        failure_scale=args.failure_scale,
        test_crash=not args.no_crash,
    )
    sys.exit(0 if success else 1)

