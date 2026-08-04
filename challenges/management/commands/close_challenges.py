import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from challenges.models import Challenge
from challenges.services import close_challenge

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Close challenges whose end_date has passed."

    def handle(self, *args, **options):
        today = timezone.localdate()
        challenges = Challenge.objects.filter(
            status=Challenge.Status.ACTIVE,
            end_date__lt=today,
        )

        count = 0
        for challenge in challenges:
            close_challenge(challenge)
            count += 1

        logger.info("close_challenges: closed %s challenge(s)", count)
        self.stdout.write(f"Closed {count} challenge(s).")
