import pytest
from django.db import IntegrityError

from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)


@pytest.mark.django_db
class TestChallengeParticipantModel:
    def test_default_custom_goal_is_null(self):
        participant = ChallengeParticipantFactory()
        assert participant.custom_goal is None
        assert participant.has_goal_configured is False

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

    def test_custom_goal_can_be_set(self):
        from challenges.tests.factories import CustomGoalFactory

        participant = ChallengeParticipantFactory()
        goal = CustomGoalFactory(participant=participant)
        participant.custom_goal = goal
        participant.save(update_fields=["custom_goal"])
        participant.refresh_from_db()
        assert participant.custom_goal_id == goal.id
        assert participant.has_goal_configured is True

    def test_rep_target_goal_can_be_set(self):
        from challenges.tests.factories import RepTargetGoalFactory

        participant = ChallengeParticipantFactory()
        goal = RepTargetGoalFactory(participant=participant)
        participant.rep_target_goal = goal
        participant.save(update_fields=["rep_target_goal"])
        participant.refresh_from_db()
        assert participant.rep_target_goal_id == goal.id
        assert participant.has_goal_configured is True
