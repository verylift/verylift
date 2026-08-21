import secrets
from decimal import Decimal

import factory
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import (
    Challenge,
    ChallengeInviteLink,
    ChallengeLift,
    ChallengeParticipant,
    CustomGoal,
    CustomGoalTarget,
    RepTargetGoal,
    RepTargetGoalTarget,
)


class ChallengeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Challenge

    name = factory.Sequence(lambda n: f"Challenge {n}")
    creator = factory.SubFactory(UserFactory)
    start_date = factory.Faker("date_object")
    end_date = factory.Faker("future_date")
    status = Challenge.Status.DRAFT


class ChallengeParticipantFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ChallengeParticipant

    challenge = factory.SubFactory(ChallengeFactory)
    user = factory.SubFactory(UserFactory)
    invite_status = ChallengeParticipant.InviteStatus.INVITED
    joined_at = None
    is_bailed = False
    bailed_at = None


class ChallengeInviteLinkFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ChallengeInviteLink

    challenge = factory.SubFactory(ChallengeFactory)
    token = factory.LazyFunction(lambda: secrets.token_urlsafe(6))
    created_by = factory.SubFactory(UserFactory)
    expires_at = factory.LazyFunction(
        lambda: timezone.now() + timezone.timedelta(days=7)
    )
    revoked_at = None
    max_uses = None
    use_count = 0


class ChallengeLiftFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ChallengeLift

    challenge = factory.SubFactory(ChallengeFactory)
    name = factory.Sequence(lambda n: f"Lift {n}")


class CustomGoalFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CustomGoal

    participant = factory.SubFactory(ChallengeParticipantFactory)
    name = factory.Sequence(lambda n: f"Custom Goal {n}")


class CustomGoalTargetFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CustomGoalTarget

    goal = factory.SubFactory(CustomGoalFactory)
    lift = "Bench Press"
    rep_count = 1
    target_weight = Decimal("100.00")


class RepTargetGoalFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RepTargetGoal

    participant = factory.SubFactory(ChallengeParticipantFactory)
    name = factory.Sequence(lambda n: f"Rep Target Goal {n}")


class RepTargetGoalTargetFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RepTargetGoalTarget

    goal = factory.SubFactory(RepTargetGoalFactory)
    lift = "Push Up"
    target_weight = Decimal("0.00")
    target_reps = 20


def make_custom_challenge(*, lifts=("Bench Press",), **kwargs):
    """Build a challenge with its configured ChallengeLift rows.

    Every challenge is CUSTOM (TASK-248); this used to also pin a source, but
    there is no longer a source to pick — the name survives as the
    conventional helper for "give this challenge a lift list".
    """
    challenge = ChallengeFactory(**kwargs)
    for name in lifts:
        ChallengeLiftFactory(challenge=challenge, name=name)
    return challenge


def make_rep_target_challenge(*, lifts=("Push Up",), **kwargs):
    """Build a REP_TARGET-mode challenge with its configured ChallengeLift rows."""
    kwargs.setdefault("mode", Challenge.Mode.REP_TARGET)
    return make_custom_challenge(lifts=lifts, **kwargs)
