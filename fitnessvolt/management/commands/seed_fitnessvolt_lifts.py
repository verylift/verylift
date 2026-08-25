import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand

from core.models import LiftAlias, LiftAliasSource

logger = logging.getLogger(__name__)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "fitnessvolt_lifts.json"
)


class Command(BaseCommand):
    help = (
        "Seed core.LiftAlias (source=fitnessvolt) slug -> canonical-lift-name "
        "rows from the fixture (idempotent)"
    )

    def handle(self, *args, **options):
        with open(FIXTURE_PATH) as f:
            data = json.load(f)

        aliases_created = 0
        for row in data["aliases"]:
            _, created = LiftAlias.objects.update_or_create(
                source=LiftAliasSource.FITNESSVOLT,
                from_name=row["from_slug"],
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
