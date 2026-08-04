import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand

from fitnessvolt.models import FitnessVoltLiftAlias

logger = logging.getLogger(__name__)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "fitnessvolt_lifts.json"
)


class Command(BaseCommand):
    help = (
        "Seed FitnessVoltLiftAlias slug -> canonical-lift-name rows from the "
        "fixture (idempotent)"
    )

    def handle(self, *args, **options):
        with open(FIXTURE_PATH) as f:
            data = json.load(f)

        aliases_created = 0
        for row in data["aliases"]:
            _, created = FitnessVoltLiftAlias.objects.update_or_create(
                from_slug=row["from_slug"],
                defaults={"to_name": row["to_name"]},
            )
            if created:
                aliases_created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {aliases_created} new FitnessVolt lift aliases "
                f"({len(data['aliases'])} total in fixture)."
            )
        )
