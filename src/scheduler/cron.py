import sys
import asyncio
import signal
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config.env import config, validate_env
from src.pipeline import run_scan
from src.utils.logger import logger

# Validate environment on startup
try:
    validate_env()
except Exception as err:
    logger.error(f"Environment validation failed: {err}")
    logger.error("Fix your .env file and restart.")
    sys.exit(1)

schedule = config["scan_cron_schedule"]

logger.info("========================================")
logger.info("COMPETITOR MONITOR — Cron Daemon Started")
logger.info("========================================")
logger.info(f"Schedule: {schedule}")
logger.info("Next run will execute the full scan pipeline.")
logger.info("Press Ctrl+C to stop.\n")

scan_running = False
scheduler = BlockingScheduler()

def trigger_scan():
    global scan_running
    if scan_running:
        logger.info("[CRON] Skipping — previous scan still running.")
        return

    scan_running = True
    logger.info("[CRON] Triggering scheduled scan...")

    # We need to run the async pipeline in an event loop
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(run_scan())
        loop.close()
        logger.info(f"[CRON] Scan complete. Changes: {result['stats']['totalChanges']}, Warnings: {len(result['warnings'])}")
    except Exception as err:
        logger.error(f"[CRON] Scan failed: {err}")
    finally:
        scan_running = False

try:
    trigger = CronTrigger.from_crontab(schedule)
    scheduler.add_job(trigger_scan, trigger)
except Exception as cron_err:
    logger.error(f"Invalid cron schedule expression: \"{schedule}\" ({cron_err})")
    logger.error("Check SCAN_CRON_SCHEDULE in your .env file.")
    sys.exit(1)

def graceful_shutdown(signum, frame):
    logger.info("[CRON] Received shutdown signal — stopping scheduler...")
    scheduler.shutdown()
    sys.exit(0)

signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

if __name__ == "__main__":
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
