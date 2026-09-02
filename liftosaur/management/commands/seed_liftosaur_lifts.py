import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand

from core.models import LiftAlias, LiftAliasSource
from liftosaur.models import Lift

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "liftosaur_lifts.json"


class Command(BaseCommand):
    help = (
        "Seed Lift (bodyweight-added quality) and LiftAlias rows from "
        "the fixture (idempotent)"
    )

    def handle(self, *args, **options):
        with open(FIXTURE_PATH) as f:
            data = json.load(f)

        lifts_created = 0
        for row in data["lifts"]:
            _, created = Lift.objects.update_or_create(
                name=row["name"],
                defaults={"is_bodyweight_added": row["is_bodyweight_added"]},
            )
            if created:
                lifts_created += 1

        aliases_created = 0
        for row in data["aliases"]:
            _, created = LiftAlias.objects.update_or_create(
                source=LiftAliasSource.LIFTOSAUR,
                from_name=row["from_name"],
                defaults={"to_name": row["to_name"]},
            )
            if created:
                aliases_created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {lifts_created} new lifts "
                f"({len(data['lifts'])} total in fixture) and "
                f"{aliases_created} new aliases "
                f"({len(data['aliases'])} total in fixture)."
            )
        )
