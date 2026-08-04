"""One-shot repair for LiftHistory rows pooled under a raw, un-aliased name.

Before the canonical_lift_name() lookup was made case-insensitive, an exercise
name whose casing differed from the seeded LiftAlias.from_name (e.g. Liftosaur
emits "Behind The Neck Press" while the fixture reads "Behind the Neck Press")
missed the exact-match lookup. The set was pooled into LiftHistory under its raw
name instead of the canonical standard name, so scoring's lift__in filter — built
from canonical names — silently excluded it: stored, but never counted.

This command repairs existing data:

1. Rewrite every LiftHistory row whose lift value case-insensitively matches an
   alias from_name but is not already the canonical to_name, setting lift to the
   canonical to_name. The (user, lift, performed_at, reps, weight_kg)
   unique_together can already hold a genuine canonical row for the same set (a
   later correctly-aliased re-sync); the raw duplicate is deleted in that case.
2. For every affected user's non-frozen challenges, re-run score_pooled_history
   so the now-canonical sets retroactively earn points. Scoring is idempotent, so
   already-scored sets are untouched.

Completed/cancelled challenges are skipped: their ledgers are frozen by design.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from challenges.models import Challenge, ChallengeParticipant
from liftosaur.models import LiftAlias, LiftHistory
from scoring.services import score_pooled_history

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Re-canonicalize LiftHistory rows stored under a raw, un-aliased name "
        "(a case-mismatch against a seeded alias), then rescore affected "
        "challenges so the recovered sets count"
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
        aliases = {
            from_name.lower(): to_name
            for from_name, to_name in LiftAlias.objects.values_list(
                "from_name", "to_name"
            )
        }

        affected_users = set()
        rewritten = 0
        deleted = 0
        with transaction.atomic():
            rows = LiftHistory.objects.select_for_update().all()
            if options["username"]:
                rows = rows.filter(user__username=options["username"])
            for row in rows.iterator():
                canonical = aliases.get(row.lift.lower())
                if canonical is None or row.lift == canonical:
                    continue

                affected_users.add(row.user_id)
                if dry_run:
                    rewritten += 1
                    self.stdout.write(
                        f"would recanonicalize user={row.user_id} "
                        f"{row.lift!r} -> {canonical!r} on {row.performed_at}"
                    )
                    continue

                collision = (
                    LiftHistory.objects.filter(
                        user_id=row.user_id,
                        lift=canonical,
                        performed_at=row.performed_at,
                        reps=row.reps,
                        weight_kg=row.weight_kg,
                    )
                    .exclude(pk=row.pk)
                    .exists()
                )
                if collision:
                    row.delete()
                    deleted += 1
                    logger.info(
                        "Deleted raw LiftHistory row %s (%s) for user %s: a "
                        "canonical %s row for the same set already exists",
                        row.pk,
                        row.lift,
                        row.user_id,
                        canonical,
                    )
                else:
                    row.lift = canonical
                    row.save(update_fields=["lift"])
                    rewritten += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {rewritten} rows would be recanonicalized across "
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
                f"Recanonicalized {rewritten} rows (deleted {deleted} duplicates) "
                f"for {len(affected_users)} users; rescored {rescored} "
                f"challenge/user slots."
            )
        )
