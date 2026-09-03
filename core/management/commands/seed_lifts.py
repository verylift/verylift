"""Seed the canonical lift register from the tracker-agnostic fixture.

Formerly ``seed_liftosaur_lifts``, which seeded the register AND Liftosaur's
alias vocabulary from one file (TASK-347). Those are different kinds of data
with different owners: the register is core product data every tracker
resolves against, while an alias table maps one tracker's raw names onto it.
Liftosaur's aliases now have their own command alongside Hevy's and Strong's
(``seed_liftosaur_lift_aliases``).
"""

import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand

from core.models import Lift

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "lifts.json"


class Command(BaseCommand):
    help = "Seed the core.Lift register and its qualities from the fixture (idempotent)"

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

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {lifts_created} new lifts "
                f"({len(data['lifts'])} total in fixture)."
            )
        )
