"""Just-in-time bodyweight collection in the goal-setup wizard (TASK-343).

The behaviour under test is "ask once, then never again": before this, the
wizard demanded a bodyweight inline on every single run for any challenge
with a bodyweight-added lift. These cover which runs ask, which reuse, and
what the answer is worth once given.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import User
from accounts.tests.factories import UserFactory
from challenges.custom_goals import grid_field_name
from challenges.goal_builders import suggest_from_history
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_custom_challenge,
)
from liftosaur.tests.factories import LiftHistoryFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_sync():
    with patch("challenges.services.sync_user_lifts"):
        yield


@pytest.fixture
def user():
    return UserFactory(unit_preference="kg")


@pytest.fixture
def authed_client(user):
    c = Client()
    c.force_login(user)
    return c


def _challenge(user, lifts):
    challenge = make_custom_challenge(
        lifts=lifts, creator=user, status=Challenge.Status.DRAFT
    )
    ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )
    return challenge


def _url(challenge):
    return reverse("challenges:goal-setup", args=[challenge.pk])


class TestWhoGetsAsked:
    def test_manual_entry_asks_when_a_bodyweight_added_lift_is_configured(
        self, authed_client, user
    ):
        # Manual entry has no derivation parameters at all, so it normally
        # skips straight to the grid. The Compute button's formula ensemble
        # on a Pull-up row is the one thing that still needs a bodyweight.
        challenge = _challenge(user, ["Pull-up"])
        authed_client.post(_url(challenge), {"method": "custom"})

        response = authed_client.get(_url(challenge))

        assert response.context["step"] == "inputs"
        assert response.context["form"].needs_bodyweight is True

    def test_manual_entry_skips_the_step_with_no_bodyweight_added_lift(
        self, authed_client, user
    ):
        challenge = _challenge(user, ["Back Squat"])
        authed_client.post(_url(challenge), {"method": "custom"})

        response = authed_client.get(_url(challenge))

        assert response.context["step"] == "chart"

    def test_a_stored_bodyweight_is_never_asked_for_again(self, authed_client, user):
        user.set_bodyweight(Decimal("82.5"), User.BodyweightSource.MANUAL)
        challenge = _challenge(user, ["Pull-up"])
        authed_client.post(_url(challenge), {"method": "custom"})

        response = authed_client.get(_url(challenge))

        assert response.context["step"] == "chart"

    def test_history_still_reaches_inputs_for_rounding_but_stops_asking(
        self, authed_client, user
    ):
        # History reaches "inputs" regardless (it needs a rounding choice);
        # only the bodyweight FIELD should disappear once one is on file.
        user.set_bodyweight(Decimal("82.5"), User.BodyweightSource.MANUAL)
        user.liftosaur_api_key = "key"
        user.save(update_fields=["liftosaur_api_key"])
        LiftHistoryFactory(user=user, lift="Pull-up", reps=5, weight_kg=Decimal("10"))
        challenge = _challenge(user, ["Pull-up"])
        authed_client.post(_url(challenge), {"method": "history"})

        response = authed_client.get(_url(challenge))

        assert response.context["step"] == "inputs"
        assert response.context["form"].needs_bodyweight is False


class TestAnswerIsKept:
    def test_answering_the_prompt_stores_it_on_the_account(self, authed_client, user):
        challenge = _challenge(user, ["Pull-up"])
        authed_client.post(_url(challenge), {"method": "custom"})

        authed_client.post(
            _url(challenge), {"wizard_step": "inputs", "bodyweight": "82.5"}
        )

        user.refresh_from_db()
        assert user.bodyweight_kg == Decimal("82.50")
        assert user.bodyweight_source == User.BodyweightSource.MANUAL

    def test_a_second_challenge_reuses_the_answer(self, authed_client, user):
        first = _challenge(user, ["Pull-up"])
        authed_client.post(_url(first), {"method": "custom"})
        authed_client.post(_url(first), {"wizard_step": "inputs", "bodyweight": "82.5"})

        second = _challenge(user, ["Pull-up"])
        authed_client.post(_url(second), {"method": "custom"})
        response = authed_client.get(_url(second))

        assert response.context["step"] == "chart"

    def test_the_compute_grid_gets_the_stored_figure_in_the_display_unit(
        self, authed_client, user
    ):
        user.unit_preference = "lb"
        user.save(update_fields=["unit_preference"])
        user.set_bodyweight(Decimal("81.65"), User.BodyweightSource.MANUAL)
        challenge = _challenge(user, ["Pull-up"])
        authed_client.post(_url(challenge), {"method": "custom"})

        response = authed_client.get(_url(challenge))

        # The Compute JS works entirely in display units, so a kg figure
        # handed to an lb grid would offset every anchor by 2.2x too little.
        assert response.context["compute_bodyweight"] == Decimal("180")


class TestSuggestFromHistoryReadsTheStoredValue:
    def _pullup_challenge(self, user):
        return make_custom_challenge(
            lifts=["Pull-up"], creator=user, status=Challenge.Status.DRAFT
        )

    def test_no_argument_falls_back_to_the_account(self, user):
        user.set_bodyweight(Decimal("80"), User.BodyweightSource.MANUAL)
        challenge = self._pullup_challenge(user)
        LiftHistoryFactory(user=user, lift="Pull-up", reps=5, weight_kg=Decimal("10"))

        table, needs_decision, _assisted = suggest_from_history(
            user, challenge, uplift=0, lookback_days=365
        )

        # With a bodyweight the lift is suggestible at all: the e1RM runs on
        # 90 kg of total load and each rung comes back as added weight.
        assert needs_decision == []
        assert table["Pull-up"][1] > 0

    def test_an_explicit_argument_still_wins(self, user):
        user.set_bodyweight(Decimal("80"), User.BodyweightSource.MANUAL)
        challenge = self._pullup_challenge(user)
        LiftHistoryFactory(user=user, lift="Pull-up", reps=5, weight_kg=Decimal("10"))

        stored, _n, _a = suggest_from_history(
            user, challenge, uplift=0, lookback_days=365
        )
        override, _n, _a = suggest_from_history(
            user, challenge, bodyweight_kg=Decimal("100"), uplift=0, lookback_days=365
        )

        assert override["Pull-up"][1] != stored["Pull-up"][1]

    def test_no_bodyweight_anywhere_still_needs_a_decision(self, user):
        challenge = self._pullup_challenge(user)
        LiftHistoryFactory(user=user, lift="Pull-up", reps=5, weight_kg=Decimal("10"))

        table, needs_decision, _assisted = suggest_from_history(
            user, challenge, uplift=0, lookback_days=365
        )

        # Never a guessed default -- an unanswered question stays an explicit
        # decision for the participant.
        assert needs_decision == ["Pull-up"]
        assert "Pull-up" not in table


class TestScoringIsUntouched:
    def test_confirming_a_goal_stores_the_same_targets_with_or_without_a_bodyweight(
        self, authed_client, user
    ):
        # TASK-343 AC#11: a bodyweight on file must not change what a manually
        # entered chart saves, and therefore cannot change what scores.
        challenge = _challenge(user, ["Pull-up"])
        authed_client.post(_url(challenge), {"method": "custom"})
        authed_client.post(
            _url(challenge), {"wizard_step": "inputs", "bodyweight": "82.5"}
        )
        fields = {grid_field_name(0, rep): "10" for rep in range(1, 11)}
        authed_client.post(_url(challenge), {"name": "Goal", **fields})

        participant = ChallengeParticipant.objects.get(challenge=challenge, user=user)
        targets = participant.custom_goal.targets.all()
        assert {t.target_weight for t in targets} == {Decimal("10.00")}
