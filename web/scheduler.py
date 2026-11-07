"""Scheduled collection, default Tuesday/Thursday 15:00 Europe/Warsaw."""
import logging
import os
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import ups_pod as core

logging.basicConfig(level=logging.INFO)
def run():
    if not (os.environ.get("UPS_CLIENT_ID") and os.environ.get("UPS_CLIENT_SECRET")):
        logging.warning("No UPS credentials configured; scheduled scan skipped")
        return
    core.konfiguruj_log(auto=True)
    code = core.przebieg("run", auto=True)
    logging.info("Scheduled UPS POD scan exit=%s", code)

if __name__ == "__main__":
    sched = BlockingScheduler(timezone="Europe/Warsaw")
    sched.add_job(run, CronTrigger(day_of_week="tue,thu",hour=15,minute=0,timezone="Europe/Warsaw"),
                  max_instances=1,coalesce=True,misfire_grace_time=1800,id="ups-pod")
    logging.info("UPS POD scheduler started: Tuesday/Thursday 15:00 Europe/Warsaw")
    sched.start()
