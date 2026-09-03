import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command

from challenges.models import Challenge, ChallengeParticipant
from core.management.commands.seed_all import SEED_COMMANDS
from core.models import LiftHistory
from scoring.models import PointEarnEvent


@pytest.mark.django_db
class TestSeedDemoDataCommand:
    def test_excluded_from_seed_all(self):
        assert "seed_demo_data" not in SEED_COMMANDS

    def test_creates_demo_users_and_every_challenge_status(self, django_user_model):
        call_command("seed_demo_data")

        demo_users = django_user_model.objects.filter(username__startswith="demo_")
        assert demo_users.count() == 5
        assert all(u.avatar for u in demo_users)
        assert all(u.avatar.name.endswith(".avif") for u in demo_users)

        statuses = set(
            Challenge.objects.filter(name__startswith="Demo").values_list(
                "status", flat=True
            )
        )
        assert statuses == {
            Challenge.Status.DRAFT,
            Challenge.Status.ACTIVE,
            Challenge.Status.COMPLETED,
            Challenge.Status.CANCELLED,
        }

    def test_demo_participants_have_a_locked_goal(self):
        """Every demo participant needs a CustomGoal, or the challenge detail
        page and goal-setup redirect are unreachable on a review instance.

        The participant-count assertion is load-bearing: create_challenge only
        adds the creator now (TASK-272 removed invite_participants), so without
        it this would pass vacuously on a one-person challenge."""
        call_command("seed_demo_data")

        for challenge in Challenge.objects.filter(name__startswith="Demo"):
            participants = list(challenge.participants.all())
            assert len(participants) == 5
            for participant in participants:
                assert participant.custom_goal_id is not None
                assert (
                    participant.invite_status
                    == ChallengeParticipant.InviteStatus.ACCEPTED
                )

    def test_active_and_completed_challenges_have_scored_history(self):
        call_command("seed_demo_data")

        for status in (Challenge.Status.ACTIVE, Challenge.Status.COMPLETED):
            challenge = Challenge.objects.get(name__startswith="Demo", status=status)
            assert PointEarnEvent.objects.filter(
                challenge=challenge, is_current_best=True
            ).exists()

    def test_idempotent_on_second_run(self, django_user_model):
        call_command("seed_demo_data")
        counts = (
            django_user_model.objects.filter(username__startswith="demo_").count(),
            Challenge.objects.filter(name__startswith="Demo").count(),
            LiftHistory.objects.filter(user__username__startswith="demo_").count(),
            PointEarnEvent.objects.filter(user__username__startswith="demo_").count(),
        )

        call_command("seed_demo_data")

        assert counts == (
            django_user_model.objects.filter(username__startswith="demo_").count(),
            Challenge.objects.filter(name__startswith="Demo").count(),
            LiftHistory.objects.filter(user__username__startswith="demo_").count(),
            PointEarnEvent.objects.filter(user__username__startswith="demo_").count(),
        )

    def test_reseed_after_db_reset_does_not_orphan_avatar_file(
        self, django_user_model, settings, tmp_path
    ):
        """A prior run's placeholder avatar can be left on disk (e.g. after a
        database reset that drops the users referencing it) at the
        deterministic avatars/demo_alex.avif path. Re-seeding into that state
        must reuse the path rather than let storage auto-rename to a new
        orphan file."""
        settings.MEDIA_ROOT = tmp_path
        stale_path = "avatars/demo_alex.avif"
        default_storage.save(stale_path, ContentFile(b"stale", name="demo_alex.avif"))

        call_command("seed_demo_data")

        alex = django_user_model.objects.get(username="demo_alex")
        assert alex.avatar.name == stale_path
        assert default_storage.open(stale_path).read() != b"stale"
