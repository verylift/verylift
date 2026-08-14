import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from challenges.models import Challenge
from challenges.services import challenge_end_instant, close_challenge

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Close challenges whose end_date has passed."

    def handle(self, *args, **options):
        now = timezone.now()
        # A loose, UTC-based pre-filter: any challenge that could actually be
        # over in *any* timezone has end_date <= today in UTC (a creator
        # ahead of UTC, e.g. UTC+14, can finish their local day while UTC's
        # own date hasn't rolled over yet). The exact per-challenge cutoff --
        # end_date's end-of-day in the creator's own pinned timezone -- is
        # then checked in Python via challenge_end_instant, since that varies
        # per row and can't be expressed as a single queryset filter.
        candidates = Challenge.objects.filter(
            status=Challenge.Status.ACTIVE,
            end_date__lte=timezone.localdate(),
        ).select_related("creator")

        count = 0
        for challenge in candidates:
            if challenge_end_instant(challenge) < now:
                close_challenge(challenge)
                count += 1

        logger.info("close_challenges: closed %s challenge(s)", count)
        self.stdout.write(f"Closed {count} challenge(s).")
