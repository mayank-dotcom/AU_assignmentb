# AI Usage Disclosure

## Tools used
- Antigravity agentic assistant (Google DeepMind)

## What I used them for
- Scaffolding unit tests for scheduling math, window edge calculations, and mock provider interaction.
- Structuring the state diagram (Mermaid) and verifying mathematical formulas for simulated clock conversions.
- Accelerating database schema boilerplate and query definitions for SQLite with WAL mode.

## What I wrote or decided myself
- **Core Architecture & Concurrency Model:** Chose SQLite with WAL mode and `BEGIN IMMEDIATE` transactions over an external orchestrator to give deterministic, sub-millisecond control under high clock speeds (1200x) without clock skew.
- **Dynamic Clock Speed Safety Buffer:** Realized during testing that at 1200x speed, 0.2s of network transit time equals ~4 simulated minutes, causing calls made near window close to arrive late at the carrier. Designed and implemented the dynamic speed-proportional buffer `max(180s, speed * 2.0s)` to reschedule edge calls safely before dispatch.
- **Ambiguous 504 Gateway Timeout Resolution:** Designed the deterministic query flow using `GET /v1/calls?request_id=...` to definitively resolve ambiguous 504 outcomes without ever risking duplicate dials.
- **Startup Crash Recovery:** Designed the reconciler and startup synchronization routines to inspect in-flight `dialing` or `active` calls against the mock provider API on reboot, ensuring resilience across arbitrary `kill -9` events.
- **DND Cascade & Rule R9 Precedence:** Defined the exact precedence order (`completed` > `exhausted` > `dnd_blocked`) ensuring that historical terminal outcomes are preserved when another lead sharing the same phone opts into DND.

## Where the AI was wrong or unhelpful
- **Transaction Handling in `conn.executescript()`:** An early scaffold wrapped `conn.executescript()` inside explicit `BEGIN IMMEDIATE;` and `conn.commit()`. In Python's `sqlite3`, `executescript()` automatically issues a `COMMIT` statement before executing, resulting in a runtime `sqlite3.OperationalError: cannot commit - no transaction is active`. Caught during initial test execution and fixed by removing the redundant manual commit.
- **Static Window Checking at High Simulation Speeds:** The initial scheduler checked whether the current simulated time was inside the window without taking network transit or clock advancement into account. While this passed at 1x speed, running at 1200x caused 422 `outside_calling_window` violations for calls dispatched seconds before window close. Rejected the static check and introduced speed-aware pre-dial buffers.
- **Column count mismatch in call outcome logging:** A generated query tuple had 14 values for 15 columns. Caught during unit test runs on the DB layer and fixed immediately.

## How I verified AI-generated code
- **Automated Test Suite:** Ran 21 comprehensive unit tests covering pure scheduling math, fake clocks, state machine transitions, global DND cascading, and webhook signature verification (`python -m pytest solution/tests/ -v`).
- **End-to-End Simulation Testing:** Ran the complete workflow against the official mock telephony provider at 1200x simulated speed across 200 leads in 3 campaigns under two configurations:
  1. Clean run (`--failure-scale 0`): Verified 0 violations in `/admin/report` and 100% exact match against `/admin/leads`.
  2. Chaos & Crash run (`--failure-scale 1.0`, mid-run process kill and restart): Verified 0 violations in `/admin/report`, correct handling of 503s, 504s, duplicate webhooks, dropped webhooks, and 100% output match.
- **Line-by-Line Code Review:** Manually inspected every module (`clock.py`, `scheduler.py`, `db.py`, `provider_client.py`, `webhook_server.py`, `dispatcher.py`) to verify type annotations, logging, and error handling paths.
