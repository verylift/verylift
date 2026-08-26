"""One-shot repair for LiftHistory rows pooled under the truncated LB_TO_KG.

TASK-325 corrected accounts.units.LB_TO_KG from the truncated
Decimal("0.453592") to the exact international factor
Decimal("0.45359237"). That was the right root-cause fix, but it changes the
weight_kg a conversion produces for the same physical set: for loads above
roughly 225 lb, at specific half-pound values, the two factors round to a
kg value 0.01 apart. LiftHistory's (user, lift, performed_at, reps,
weight_kg) identity means a row already pooled under the old factor no
longer matches the SAME physical set arriving again through a source that
now converts with the exact factor -- a CSV re-import, or a Hevy API sync of
a set previously imported from Hevy's CSV export -- producing a second row
instead of an upsert. See liftosaur/lb_conversion_repair.py for the full
reasoning on which rows this can and can't identify.

Only sources that ever run a weight through LB_TO_KG are candidates:
HEVY and STRONG (workout_imports CSV importers -- their export files carry
lbs only, unconditionally converted) and LIFTOSAUR and WGER (API/CSV syncs
that convert only when the synced set's recorded unit is lb; that unit isn't
stored on the row, so it can't be read back off it). HEVY_API is never a
candidate: it takes weight_kg directly from Hevy's API, no conversion ever
runs. MANUAL is never a candidate: a manual rep-target report copies an
existing CustomGoalTarget.target_weight rather than converting a freshly
reported weight.

This command repairs existing data:

1. For every LiftHistory row from a candidate source, ask
   lb_conversion_repair.corrected_weight_kg() whether the stored weight_kg is
   confidently a mis-converted half-pound lb value. Rows it can't confidently
   identify (most rows, including every LIFTOSAUR/WGER row that was actually
   kg-native) are left untouched. Where it is confident, rewrite weight_kg to
   the exact-factor value. The (user, lift, performed_at, reps, weight_kg)
   unique_together can already hold a correct row for the same set (a later
   re-sync that already ran the exact factor); the stale row is deleted in
   that case rather than orphaned.
2. For every affected user's non-frozen challenges, re-run
   score_pooled_history so the corrected sets are scored under their new
   identity. Scoring is idempotent, so already-scored sets are untouched.

Completed/cancelled challenges are skipped: their ledgers are frozen by
design.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from challenges.models import Challenge, ChallengeParticipant
from liftosaur.lb_conversion_repair import corrected_weight_kg
from liftosaur.models import LiftHistory, LiftSource
from scoring.services import score_pooled_history

logger = logging.getLogger(__name__)

_CANDIDATE_SOURCES = (
    LiftSource.HEVY,
    LiftSource.STRONG,
    LiftSource.LIFTOSAUR,
    LiftSource.WGER,
)


class Command(BaseCommand):
    help = (
        "Restamp LiftHistory rows that were pooled under the pre-TASK-325 "
        "truncated LB_TO_KG factor, then rescore affected challenges"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            help="Repair only this user (default: every affected user)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        affected_users = set()
        rewritten = 0
        deleted = 0
        with transaction.atomic():
            rows = LiftHistory.objects.select_for_update().filter(
                source__in=_CANDIDATE_SOURCES
            )
            if options["username"]:
                rows = rows.filter(user__username=options["username"])
            for row in rows.iterator():
                new_weight_kg = corrected_weight_kg(row.weight_kg)
                if new_weight_kg is None:
                    continue

                affected_users.add(row.user_id)
                if dry_run:
                    rewritten += 1
                    self.stdout.write(
                        f"would restamp user={row.user_id} {row.lift!r} "
                        f"{row.weight_kg} -> {new_weight_kg} on {row.performed_at} "
                        f"(source={row.source})"
                    )
                    continue

                collision = (
                    LiftHistory.objects.filter(
                        user_id=row.user_id,
                        lift=row.lift,
                        performed_at=row.performed_at,
                        reps=row.reps,
                        weight_kg=new_weight_kg,
                    )
                    .exclude(pk=row.pk)
                    .exists()
                )
                if collision:
                    row.delete()
                    deleted += 1
                    logger.info(
                        "Deleted stale LiftHistory row %s (%s kg) for user %s: "
                        "a correct %s kg row for the same set already exists",
                        row.pk,
                        row.weight_kg,
                        row.user_id,
                        new_weight_kg,
                    )
                else:
                    old_weight_kg = row.weight_kg
                    row.weight_kg = new_weight_kg
                    row.save(update_fields=["weight_kg"])
                    rewritten += 1
                    logger.info(
                        "Restamped LiftHistory row %s for user %s: %s kg -> %s kg",
                        row.pk,
                        row.user_id,
                        old_weight_kg,
                        new_weight_kg,
                    )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {rewritten} rows would be restamped across "
                    f"{len(affected_users)} users (no rescore performed)."
                )
            )
            return

        rescored = 0
        for participation in (
            ChallengeParticipant.objects.filter(user_id__in=affected_users)
            .select_related("challenge", "user")
            .iterator()
        ):
            challenge = participation.challenge
            if challenge.status in (
                Challenge.Status.COMPLETED,
                Challenge.Status.CANCELLED,
            ):
                logger.info(
                    "Skipping frozen challenge %s (status=%s): ledger is read-only",
                    challenge.pk,
                    challenge.status,
                )
                continue
            summary = score_pooled_history(user=participation.user, challenge=challenge)
            rescored += 1
            logger.info(
                "Rescored challenge %s for user %s: %s sets evaluated, %s new events",
                challenge.pk,
                participation.user_id,
                summary.sets_evaluated,
                summary.new_point_events,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Restamped {rewritten} rows (deleted {deleted} duplicates) "
                f"for {len(affected_users)} users; rescored {rescored} "
                f"challenge/user slots."
            )
        )
