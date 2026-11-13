"""
CLI Entrypoint for Durable Campaign Dispatcher.
Starts the webhook receiver server and runs the dispatcher engine until completion.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from .config import SystemConfig
from .db import get_lead_status_summary, init_db, seed_fixtures
from .dispatcher import DispatcherEngine
from .provider_client import ProviderClient
from .webhook_server import create_webhook_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def parse_args():
    parser = argparse.ArgumentParser(description="Agentic Universe - Durable Campaign Dispatcher")
    parser.add_argument(
        "--fixtures",
        default=os.path.join(os.path.dirname(__file__), "..", "assignment-b-workflow", "fixtures", "campaigns.json"),
        help="Path to fixtures campaigns.json",
    )
    parser.add_argument(
        "--provider-url",
        default="http://127.0.0.1:8099",
        help="Telephony provider base URL",
    )
    parser.add_argument(
        "--db",
        default="dispatcher.db",
        help="SQLite database file path",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Webhook receiver host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8200,
        help="Webhook receiver port",
    )
    parser.add_argument(
        "--output",
        default="final_state.csv",
        help="Output final_state.csv path",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Delete existing database before running",
    )
    return parser.parse_args()


async def run_app(args):
    db_path = os.path.abspath(args.db)
    if args.reset_db and os.path.exists(db_path):
        os.remove(db_path)
        logger.info(f"Existing database removed: {db_path}")

    # Load configuration and fixtures
    fixtures_path = os.path.abspath(args.fixtures)
    if not os.path.exists(fixtures_path):
        logger.error(f"Fixtures file not found: {fixtures_path}")
        sys.exit(1)

    config = SystemConfig.load(fixtures_path)
    leads = config.load_leads()
    logger.info(f"Loaded {len(leads)} leads across {len(config.campaigns)} campaigns")

    # Initialize DB and seed
    init_db(db_path)
    seed_fixtures(db_path, config, leads)

    # Initial lead status
    summary = get_lead_status_summary(db_path)
    logger.info(f"Initial lead status: {summary}")

    callback_url = f"http://{args.host}:{args.port}/webhooks/telephony"
    client = ProviderClient(base_url=args.provider_url)

    # Setup webhook server app
    webhook_app = create_webhook_app(db_path, config)
    server_cfg = uvicorn.Config(
        app=webhook_app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(server_cfg)

    # Setup dispatcher
    engine = DispatcherEngine(
        db_path=db_path,
        config=config,
        client=client,
        callback_url=callback_url,
        output_csv_path=args.output,
    )

    # Run webhook server and dispatcher in parallel
    server_task = asyncio.create_task(server.serve())
    dispatcher_task = asyncio.create_task(engine.run())

    try:
        await dispatcher_task
        logger.info("Dispatcher finished successfully.")
    except asyncio.CancelledError:
        logger.info("Dispatcher cancelled.")
    finally:
        server.should_exit = True
        await server_task


def main():
    args = parse_args()
    try:
        asyncio.run(run_app(args))
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.")


if __name__ == "__main__":
    main()

