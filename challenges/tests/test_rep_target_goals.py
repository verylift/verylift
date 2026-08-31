"""Tests for Rep Target goal-setup parsing/persistence and the goal-setup view
(issue #85)."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant, RepTargetGoal
from challenges.rep_target_goals import (
    detach_active_rep_target_goal,
    merge_suggested_fields,
    parse_rep_target_grid,
    parse_suggested_fields,
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

    @pytest.mark.parametrize("raw_weight", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_weight_is_an_error_not_a_crash(self, raw_weight):
        # Regression: Decimal("NaN") parses fine but raised InvalidOperation
        # in the positivity check (regular lifts) or in from_display_weight's
        # quantize (bodyweight-added lifts) -- a 500 either way. One row of
        # each kind so both branches are exercised.
        challenge = make_rep_target_challenge(lifts=["Deadlift", LIFT])
        post = {}
        for lift_index in (0, 1):
            weight_field, reps_field = rep_target_field_names(lift_index)
            post[weight_field] = raw_weight
            post[reps_field] = "5"
        targets, errors = parse_rep_target_grid(post, challenge, "kg")
        assert targets == {}
        assert len(errors) == 2


class TestMergeSuggestedFields:
    def test_typed_values_are_pinned_and_blanks_filled(self):
        challenge = make_rep_target_challenge(lifts=["Dip", LIFT])
        dip_weight, dip_reps = rep_target_field_names(0)
        pu_weight, pu_reps = rep_target_field_names(1)
        suggested = {"Dip": (Decimal("10"), 12), LIFT: (Decimal("0"), 30)}

        values, suggested_fields = merge_suggested_fields(
            {dip_weight: "7.5", pu_reps: "25"}, suggested, challenge, "kg"
        )

        assert values[dip_weight] == "7.5"
        assert values[dip_reps] == "12"
        assert values[pu_weight] == "0"
        assert values[pu_reps] == "25"
        assert suggested_fields == {dip_reps, pu_weight}

    def test_lift_without_suggestion_or_input_stays_blank(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        weight_field, reps_field = rep_target_field_names(0)
        values, suggested_fields = merge_suggested_fields({}, {}, challenge, "kg")
        assert weight_field not in values
        assert reps_field not in values
        assert suggested_fields == set()

    def test_parse_suggested_fields_drops_non_grid_names(self):
        post = {"suggested_fields": "target_reps__0,<script>alert(1)</script>,name"}
        assert parse_suggested_fields(post) == {"target_reps__0"}


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

    def test_max_length_name_survives_the_rename(self):
        # Regression: appending " [uuid]" to a 62+ char goal name overflowed
        # name's max_length=100 and 500'd the leave/bail path.
        challenge = make_rep_target_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        goal = RepTargetGoalFactory(participant=participant, name="x" * 100)
        participant.rep_target_goal = goal
        participant.save(update_fields=["rep_target_goal"])

        detach_active_rep_target_goal(participant)

        goal.refresh_from_db()
        assert len(goal.name) <= 100
        assert goal.name.endswith(f"[{goal.id}]")


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

    def test_blank_name_is_rejected_rather_than_defaulted(self):
        # The field is neither prefilled nor defaulted at save time any more:
        # a blank (or whitespace-only) name must come back as an error, or
        # every chart ends up named alike.
        challenge = make_rep_target_challenge(lifts=[LIFT])
        user = UserFactory()
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        weight_field, reps_field = rep_target_field_names(0)
        response = client.post(
            reverse("challenges:goal-setup", args=[challenge.pk]),
            {"name": "   ", "action": "save", weight_field: "0", reps_field: "20"},
        )
        assert response.status_code == 200
        assert response.context["errors"]
        participant.refresh_from_db()
        assert not participant.has_goal_configured

    def test_suggest_does_not_demand_a_name(self):
        # "Suggest targets" re-renders the same form rather than saving, so the
        # name requirement must not fire on it -- filling the grid first and
        # naming the goal last is a normal order to work in.
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
            {"name": "", "action": "suggest"},
        )
        assert response.status_code == 200
        assert not response.context["errors"]

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

    def test_over_long_name_is_an_error_not_a_crash(self):
        # The template's maxlength stops honest input at 100; a crafted POST
        # used to reach RepTargetGoal.objects.create and 500 with DataError.
        challenge = make_rep_target_challenge(lifts=[LIFT])
        user = UserFactory()
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        weight_field, reps_field = rep_target_field_names(0)
        response = client.post(
            reverse("challenges:goal-setup", args=[challenge.pk]),
            {"name": "x" * 101, "action": "save", weight_field: "0", reps_field: "20"},
        )
        assert response.status_code == 200
        assert response.context["errors"]
        participant.refresh_from_db()
        assert not participant.has_goal_configured

    def test_suggest_pins_typed_values_and_fills_only_blanks(self):
        # A field the participant already filled must survive Suggest
        # untouched; only the blank fields take the suggestion, and only
        # those are flagged as suggested for the grid styling.
        challenge = make_rep_target_challenge(lifts=[LIFT])
        user = UserFactory(unit_preference="kg")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        weight_field, reps_field = rep_target_field_names(0)
        with patch(
            "challenges.views.suggest_rep_targets_from_history",
            return_value=({LIFT: (Decimal("5"), 30)}, []),
        ):
            response = client.post(
                reverse("challenges:goal-setup", args=[challenge.pk]),
                {"name": "My Goal", "action": "suggest", reps_field: "25"},
            )
        assert response.status_code == 200
        row = response.context["lifts"][0]
        assert row["reps_value"] == "25"
        assert not row["reps_suggested"]
        assert row["weight_value"] == "5"
        assert row["weight_suggested"]
        assert response.context["suggested_fields"] == weight_field

    def test_second_suggest_keeps_first_suggestions_marked(self):
        # Regression: a field the first Suggest filled comes back in the
        # second Suggest's POST as an ordinary value, so the merge saw it as
        # typed and its suggested styling vanished. The round-tripped
        # suggested_fields hidden input has to keep it marked.
        challenge = make_rep_target_challenge(lifts=[LIFT])
        user = UserFactory(unit_preference="kg")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        weight_field, reps_field = rep_target_field_names(0)
        with patch(
            "challenges.views.suggest_rep_targets_from_history",
            return_value=({LIFT: (Decimal("5"), 30)}, []),
        ):
            response = client.post(
                reverse("challenges:goal-setup", args=[challenge.pk]),
                {
                    "name": "My Goal",
                    "action": "suggest",
                    weight_field: "5",
                    reps_field: "30",
                    "suggested_fields": f"{weight_field},{reps_field}",
                },
            )
        row = response.context["lifts"][0]
        assert row["weight_suggested"]
        assert row["reps_suggested"]
        assert set(response.context["suggested_fields"].split(",")) == {
            weight_field,
            reps_field,
        }

    def test_save_records_history_provenance_when_suggested(self):
        # Classic's wizard persists HISTORY for suggested goals; the grid's
        # suggested_fields hidden input carries the same fact here.
        challenge = make_rep_target_challenge(lifts=[LIFT])
        user = UserFactory()
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        weight_field, reps_field = rep_target_field_names(0)
        response = client.post(
            reverse("challenges:goal-setup", args=[challenge.pk]),
            {
                "name": "My Goal",
                "action": "save",
                weight_field: "0",
                reps_field: "20",
                "suggested_fields": f"{weight_field},{reps_field}",
            },
        )
        assert response.status_code == 302
        participant.refresh_from_db()
        assert (
            participant.rep_target_goal.source_method
            == RepTargetGoal.SourceMethod.HISTORY
        )

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

    def test_suggest_action_toasts_lifts_with_no_history(self):
        """UAT feedback: a full-width "no recent history" row broke the
        grid's spacing -- it's a toast instead (issue #85 follow-up)."""
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
            {"name": "My Goal", "action": "suggest"},
        )
        content = response.content.decode()
        assert f"No recent history for {LIFT}" in content
        # The old per-row message is gone -- it broke the grid's spacing.
        assert "No recent history for this lift yet" not in content
