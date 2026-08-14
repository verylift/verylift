import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def close_challenges_job():
    logger.info("run_scheduler: invoking close_challenges")
    call_command("close_challenges")


def build_scheduler():
    scheduler = BlockingScheduler()
    scheduler.add_job(
        close_challenges_job,
        trigger=CronTrigger(minute="0,30"),
        id="close_challenges",
        max_instances=1,
    )
    logger.info(
        "run_scheduler: registered close_challenges job (cron minute=0,30, "
        "max_instances=1)"
    )
    return scheduler


class Command(BaseCommand):
    help = "Run an in-process scheduler that closes challenges every 30 minutes."

    def handle(self, *args, **options):
        scheduler = build_scheduler()
        logger.info("run_scheduler: starting blocking scheduler")
        scheduler.start()
