import pytest
from django.db import IntegrityError
from django.utils import timezone

from challenges.models import ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)


@pytest.mark.django_db
class TestChallengeParticipantModel:
    def test_has_uuid_pk(self):
        participant = ChallengeParticipantFactory()
        assert participant.pk is not None
        assert len(str(participant.pk)) == 36

    def test_default_invite_status_is_invited(self):
        participant = ChallengeParticipantFactory()
        assert participant.invite_status == ChallengeParticipant.InviteStatus.INVITED

    def test_default_is_bailed_false(self):
        participant = ChallengeParticipantFactory()
        assert participant.is_bailed is False

    def test_default_custom_goal_is_null(self):
        participant = ChallengeParticipantFactory()
        assert participant.custom_goal is None
        assert participant.has_goal_configured is False

    def test_default_joined_at_is_null(self):
        participant = ChallengeParticipantFactory()
        assert participant.joined_at is None

    def test_default_bailed_at_is_null(self):
        participant = ChallengeParticipantFactory()
        assert participant.bailed_at is None

    def test_str_representation(self):
        participant = ChallengeParticipantFactory()
        assert str(participant.user) in str(participant)
        assert str(participant.challenge) in str(participant)

    def test_unique_together_enforced(self):
        challenge = ChallengeFactory()
        from accounts.tests.factories import UserFactory

        user = UserFactory()
        ChallengeParticipantFactory(challenge=challenge, user=user)
        with pytest.raises(IntegrityError):
            ChallengeParticipantFactory(challenge=challenge, user=user)

    def test_bailed_state_sets_is_bailed_and_bailed_at(self):
        now = timezone.now()
        participant = ChallengeParticipantFactory(is_bailed=True, bailed_at=now)
        assert participant.is_bailed is True
        assert participant.bailed_at == now

    def test_custom_goal_can_be_set(self):
        from challenges.tests.factories import CustomGoalFactory

        participant = ChallengeParticipantFactory()
        goal = CustomGoalFactory(participant=participant)
        participant.custom_goal = goal
        participant.save(update_fields=["custom_goal"])
        participant.refresh_from_db()
        assert participant.custom_goal_id == goal.id
        assert participant.has_goal_configured is True
