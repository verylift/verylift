import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand

from core.models import LiftAlias, LiftAliasSource

logger = logging.getLogger(__name__)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "hevy_lift_aliases.json"
)


class Command(BaseCommand):
    help = (
        "Seed core.LiftAlias (source=hevy) raw-name -> canonical-lift-name "
        "rows from the fixture (idempotent)"
    )

    def handle(self, *args, **options):
        with open(FIXTURE_PATH) as f:
            data = json.load(f)

        aliases_created = 0
        for row in data["aliases"]:
            _, created = LiftAlias.objects.update_or_create(
                source=LiftAliasSource.HEVY,
                from_name=row["from_name"],
                defaults={"to_name": row["to_name"]},
            )
            if created:
                aliases_created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {aliases_created} new Hevy lift aliases "
                f"({len(data['aliases'])} total in fixture)."
            )
        )
