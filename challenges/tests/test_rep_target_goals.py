"""Tests for Rep Target goal-setup parsing/persistence and the goal-setup view
(issue #85)."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.rep_target_goals import (
    detach_active_rep_target_goal,
    parse_rep_target_grid,
    rep_target_field_names,
    rep_target_goal_is_complete,
    save_rep_target_goal,
)
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    RepTargetGoalFactory,
    make_rep_target_challenge,
)

pytestmark = pytest.mark.django_db

LIFT = "Push Up"


@pytest.fixture(autouse=True)
def _no_sync():
    with patch("challenges.services.sync_user_lifts"):
        yield


class TestParseRepTargetGrid:
    def test_parses_complete_row(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        weight_field, reps_field = rep_target_field_names(0)
        targets, errors = parse_rep_target_grid(
            {weight_field: "0", reps_field: "20"}, challenge, "kg"
        )
        assert errors == []
        assert targets == {LIFT: (Decimal("0"), 20)}

    def test_blank_row_is_left_out_not_an_error(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        targets, errors = parse_rep_target_grid({}, challenge, "kg")
        assert errors == []
        assert targets == {}

    def test_non_positive_weight_rejected_for_non_bodyweight_lift(self):
        challenge = make_rep_target_challenge(lifts=["Deadlift"])
        weight_field, reps_field = rep_target_field_names(0)
        targets, errors = parse_rep_target_grid(
            {weight_field: "0", reps_field: "5"}, challenge, "kg"
        )
        assert "Deadlift" not in targets
        assert errors

    def test_zero_weight_accepted_for_bodyweight_added_lift(self):
        challenge = make_rep_target_challenge(lifts=["Pull-up"])
        weight_field, reps_field = rep_target_field_names(0)
        targets, errors = parse_rep_target_grid(
            {weight_field: "0", reps_field: "5"}, challenge, "kg"
        )
        assert errors == []
        assert targets == {"Pull-up": (Decimal("0"), 5)}

    @pytest.mark.parametrize("raw_reps", ["0", "1000", "abc", ""])
    def test_invalid_rep_count_rejected(self, raw_reps):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        weight_field, reps_field = rep_target_field_names(0)
        targets, errors = parse_rep_target_grid(
            {weight_field: "0", reps_field: raw_reps}, challenge, "kg"
        )
        assert LIFT not in targets
        assert errors


class TestRepTargetGoalIsComplete:
    def test_missing_lift_reported(self):
        challenge = make_rep_target_challenge(lifts=[LIFT, "Dip"])
        errors = rep_target_goal_is_complete({LIFT: (Decimal("0"), 10)}, challenge)
        assert len(errors) == 1
        assert "Dip" in errors[0]

    def test_complete_table_has_no_errors(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        errors = rep_target_goal_is_complete({LIFT: (Decimal("0"), 10)}, challenge)
        assert errors == []


class TestSaveRepTargetGoal:
    def test_saves_goal_and_locks_participant(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        goal = save_rep_target_goal(participant, "My Goal", {LIFT: (Decimal("0"), 20)})
        participant.refresh_from_db()
        assert participant.rep_target_goal_id == goal.pk
        target = goal.targets.get(lift=LIFT)
        assert target.target_weight == Decimal("0.00")
        assert target.target_reps == 20

    def test_raises_when_participant_already_has_a_goal(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        save_rep_target_goal(participant, "First", {LIFT: (Decimal("0"), 20)})
        with pytest.raises(ValueError):
            save_rep_target_goal(participant, "Second", {LIFT: (Decimal("0"), 10)})


class TestDetachActiveRepTargetGoal:
    def test_detaches_and_frees_the_name_for_rejoin(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        goal = RepTargetGoalFactory(participant=participant, name="My Goal")
        participant.rep_target_goal = goal
        participant.save(update_fields=["rep_target_goal"])

        detach_active_rep_target_goal(participant)

        assert participant.rep_target_goal is None
        goal.refresh_from_db()
        assert goal.name != "My Goal"
        # The original name is free again for a fresh goal on rejoin.
        RepTargetGoalFactory(participant=participant, name="My Goal")

    def test_noop_when_no_goal_configured(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(challenge=challenge)
        detach_active_rep_target_goal(participant)
        assert participant.rep_target_goal_id is None


class TestRepTargetGoalSetupView:
    def test_get_renders_one_row_per_lift(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        user = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        response = client.get(reverse("challenges:goal-setup", args=[challenge.pk]))
        assert response.status_code == 200
        assert response.context["lifts"][0]["name"] == LIFT

    def test_post_save_locks_goal_and_activates_draft(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        creator = UserFactory()
        challenge.creator = creator
        challenge.save(update_fields=["creator"])
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=creator,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(creator)
        weight_field, reps_field = rep_target_field_names(0)
        response = client.post(
            reverse("challenges:goal-setup", args=[challenge.pk]),
            {"name": "My Goal", "action": "save", weight_field: "0", reps_field: "20"},
        )
        assert response.status_code == 302
        participant.refresh_from_db()
        assert participant.has_goal_configured
        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.ACTIVE

    def test_post_incomplete_reprompts_with_errors(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        user = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        response = client.post(
            reverse("challenges:goal-setup", args=[challenge.pk]),
            {"name": "My Goal", "action": "save"},
        )
        assert response.status_code == 200
        assert response.context["errors"]

    def test_suggest_action_prefills_without_saving(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        user = UserFactory()
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        response = client.post(
            reverse("challenges:goal-setup", args=[challenge.pk]),
            {"name": "My Goal", "action": "suggest"},
        )
        assert response.status_code == 200
        participant.refresh_from_db()
        assert not participant.has_goal_configured
