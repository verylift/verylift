import logging

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

SEED_COMMANDS = (
    "seed_liftosaur_lifts",
    "seed_fitnessvolt_lifts",
)


class Command(BaseCommand):
    help = (
        "Run every idempotent seed command (Liftosaur lifts, FitnessVolt lift "
        "aliases) so the DB matches the fixtures shipped in the image. Safe to "
        "run on every deploy."
    )

    def handle(self, *args, **options):
        for name in SEED_COMMANDS:
            logger.info("Running seed command %s", name)
            try:
                call_command(name, stdout=self.stdout, stderr=self.stderr)
            except Exception:
                logger.exception("Seed command %s failed", name)
                raise
        logger.info("All seed commands completed")
        self.stdout.write(self.style.SUCCESS("All seed commands completed."))
