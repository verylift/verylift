"""Operator-run full pull of the FitnessVolt standards cache (doc-1 §3).

Manual, not scheduled: run once to enable FitnessVolt for the first time,
and again whenever a new FitnessVolt data_version should be picked up.
Idempotent — a run with no new data_version published is a no-op, and
garbage collection of stale unreferenced snapshots rides along only when a
genuinely new snapshot is inserted.
"""

import logging

from django.core.management.base import BaseCommand

from fitnessvolt.services import refresh_cache

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Full pull of FitnessVolt strength standards for both populations "
        "into the versioned cache (idempotent; no-op when data_version is "
        "unchanged)"
    )

    def handle(self, *args, **options):
        logger.info("Starting FitnessVolt cache refresh (full pull, both populations)")
        summary = refresh_cache()
        if not summary:
            self.stdout.write(
                self.style.WARNING(
                    "No populations refreshed — see logs for the failure cause."
                )
            )
            return
        for population, outcome in summary.items():
            self.stdout.write(self.style.SUCCESS(f"{population}: {outcome}"))
