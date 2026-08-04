import factory
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
    CustomGoalFactory,
    CustomGoalTargetFactory,
    make_custom_challenge,
)
from scoring.models import PointEarnEvent


def make_custom_scoring_setup(
    *,
    lift,
    targets,
    is_bailed=False,
    history_window=None,
    start_date=None,
    end_date=None,
):
    """Build an accepted participant with a complete custom goal for one lift.

    Every challenge is CUSTOM (TASK-248): this is the only scoring setup left
    in the test suite. ``targets`` is a ``{rep_count: kg}`` mapping (kg
    Decimals). Returns ``(user, challenge, participant)`` ready for scoring.
    """
    user = UserFactory()
    challenge_kwargs = {}
    if history_window is not None:
        challenge_kwargs["history_window"] = history_window
    if start_date is not None:
        challenge_kwargs["start_date"] = start_date
    if end_date is not None:
        challenge_kwargs["end_date"] = end_date
    challenge = make_custom_challenge(lifts=[lift], **challenge_kwargs)
    participant = ChallengeParticipantFactory(
        user=user,
        challenge=challenge,
        is_bailed=is_bailed,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )
    goal = CustomGoalFactory(participant=participant, name="My Targets")
    for rep_count, weight in targets.items():
        CustomGoalTargetFactory(
            goal=goal, lift=lift, rep_count=rep_count, target_weight=weight
        )
    participant.custom_goal = goal
    participant.save(update_fields=["custom_goal"])
    return user, challenge, participant


class PointEarnEventFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PointEarnEvent

    user = factory.SubFactory(UserFactory)
    challenge = factory.SubFactory(ChallengeFactory)
    lift = "Squat"
    performed_at = factory.Faker("date_object")
    synced_at = factory.LazyFunction(timezone.now)
    reps = 5
    weight = factory.Faker("pydecimal", left_digits=3, right_digits=2, positive=True)
    points_earned = 6
    is_current_best = True
