"""Tests for custom-goal setup and editing (TASK-134).

Covers the JSON and manual-grid parsing/validation vocabulary and the
setup/edit view flow (completion gate, draft activation, prospective edits).
"""

import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import NoReverseMatch, reverse

from accounts.tests.factories import UserFactory
from challenges.custom_goals import (
    custom_goal_is_complete,
    grid_field_name,
    parse_custom_goal_grid,
    parse_custom_goal_json,
    save_custom_goal,
    validate_rep_max_monotonicity,
)
from challenges.forms import CustomGoalForm
from challenges.models import (
    Challenge,
    ChallengeParticipant,
    CustomGoal,
    CustomGoalTarget,
)
from challenges.services import build_custom_goal_context
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_custom_challenge,
)

pytestmark = pytest.mark.django_db


LIFT = "Bench Press"


@pytest.fixture(autouse=True)
def _no_sync():
    with patch("challenges.services.sync_user_lifts"):
        yield


@pytest.fixture
def user():
    return UserFactory(unit_preference="kg")


@pytest.fixture
def challenge(user):
    return make_custom_challenge(
        lifts=[LIFT], creator=user, status=Challenge.Status.DRAFT
    )


@pytest.fixture
def participant(challenge, user):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )


@pytest.fixture
def authed_client(user):
    client = Client()
    client.force_login(user)
    return client


def full_json(unit="kg", weights=None, name="My Goal"):
    weights = weights or {str(rep): 100 - rep for rep in range(1, 11)}
    payload = {"unit": unit, "targets": {LIFT: weights}}
    if name is not None:
        payload["name"] = name
    return json.dumps(payload)


def grid_post(index=0, weights=None):
    weights = weights or {rep: 100 - rep for rep in range(1, 11)}
    return {grid_field_name(index, rep): str(w) for rep, w in weights.items()}


class TestParseJson:
    def test_kg_payload_stored_as_kg(self, challenge):
        name, targets, errors = parse_custom_goal_json(full_json("kg"), challenge, "kg")
        assert errors == []
        assert name == "My Goal"
        assert targets[LIFT][1] == Decimal("99.00")
        assert set(targets[LIFT]) == set(range(1, 11))

    def test_lb_payload_converted_to_kg(self, challenge):
        payload = json.dumps(
            {"name": "Goal", "unit": "lb", "targets": {LIFT: {"1": 100}}}
        )
        _name, targets, errors = parse_custom_goal_json(payload, challenge, "kg")
        assert errors == []
        # 100 lb ≈ 45.36 kg
        assert targets[LIFT][1] == Decimal("45.36")

    def test_unit_defaults_to_participant_unit(self, challenge):
        payload = json.dumps({"name": "Goal", "targets": {LIFT: {"1": 100}}})
        _name, targets, errors = parse_custom_goal_json(payload, challenge, "lb")
        assert errors == []
        assert targets[LIFT][1] == Decimal("45.36")

    def test_malformed_json_reports_error(self, challenge):
        _name, targets, errors = parse_custom_goal_json("{not json", challenge, "kg")
        assert targets == {}
        assert any("valid JSON" in e for e in errors)

    def test_missing_name_reported(self, challenge):
        payload = json.dumps({"targets": {LIFT: {"1": 100}}})
        name, _targets, errors = parse_custom_goal_json(payload, challenge, "kg")
        assert name == ""
        assert any('"name"' in e for e in errors)

    def test_blank_name_reported(self, challenge):
        payload = json.dumps({"name": "   ", "targets": {LIFT: {"1": 100}}})
        name, _targets, errors = parse_custom_goal_json(payload, challenge, "kg")
        assert name == ""
        assert any('"name"' in e for e in errors)

    def test_unknown_lift_reported(self, challenge):
        payload = json.dumps({"name": "Goal", "targets": {"Overhead Press": {"1": 50}}})
        _name, _targets, errors = parse_custom_goal_json(payload, challenge, "kg")
        assert any("Unknown lift" in e for e in errors)

    def test_non_positive_weight_reported(self, challenge):
        payload = json.dumps(
            {"name": "Goal", "targets": {LIFT: {"1": 0, "2": -5, "3": "abc"}}}
        )
        _name, targets, errors = parse_custom_goal_json(payload, challenge, "kg")
        assert len([e for e in errors if "positive number" in e]) == 3
        assert 1 not in targets.get(LIFT, {})

    def test_bad_unit_reported_and_falls_back(self, challenge):
        payload = json.dumps(
            {"name": "Goal", "unit": "stone", "targets": {LIFT: {"1": 100}}}
        )
        _name, targets, errors = parse_custom_goal_json(payload, challenge, "kg")
        assert any("Unknown unit" in e for e in errors)
        assert targets[LIFT][1] == Decimal("100.00")

    def test_top_level_non_object_reported(self, challenge):
        _name, targets, errors = parse_custom_goal_json("[1, 2, 3]", challenge, "kg")
        assert targets == {}
        assert any('"targets"' in e for e in errors)

    def test_targets_not_an_object_reported(self, challenge):
        payload = json.dumps({"name": "Goal", "targets": "everything"})
        _name, targets, errors = parse_custom_goal_json(payload, challenge, "kg")
        assert targets == {}
        assert any("mapping lift names" in e for e in errors)

    def test_lift_cells_not_an_object_reported(self, challenge):
        payload = json.dumps({"name": "Goal", "targets": {LIFT: 100}})
        _name, _targets, errors = parse_custom_goal_json(payload, challenge, "kg")
        assert any("object of rep count to weight" in e for e in errors)


class TestCompleteness:
    def test_missing_rep_counts_reported(self, challenge):
        _name, targets, _errors = parse_custom_goal_json(
            json.dumps({"name": "Goal", "targets": {LIFT: {"1": 100, "2": 95}}}),
            challenge,
            "kg",
        )
        missing = custom_goal_is_complete(targets, challenge)
        assert len(missing) == 1
        assert "3RM" in missing[0] and "10RM" in missing[0]

    def test_missing_lift_reported(self, challenge):
        missing = custom_goal_is_complete({}, challenge)
        assert any(LIFT in e for e in missing)

    def test_complete_table_has_no_errors(self, challenge):
        _name, targets, _errors = parse_custom_goal_json(full_json(), challenge, "kg")
        assert custom_goal_is_complete(targets, challenge) == []


class TestRepMaxMonotonicity:
    def test_non_increasing_table_has_no_errors(self, challenge):
        targets = {LIFT: {rep: Decimal(100 - rep) for rep in range(1, 11)}}
        assert validate_rep_max_monotonicity(targets, challenge) == []

    def test_equal_adjacent_reps_allowed(self, challenge):
        # A genuinely rep-independent max: 5RM == 6RM is valid, not a violation.
        weights = {rep: Decimal(100 - rep) for rep in range(1, 11)}
        weights[6] = weights[5]
        targets = {LIFT: weights}
        assert validate_rep_max_monotonicity(targets, challenge) == []

    def test_heavier_higher_rep_rejected(self, challenge):
        weights = {rep: Decimal(100 - rep) for rep in range(1, 11)}
        weights[5] = Decimal("200")  # 5RM heavier than 1RM-4RM
        targets = {LIFT: weights}
        errors = validate_rep_max_monotonicity(targets, challenge)
        assert len(errors) == 1
        assert "5RM" in errors[0] and "4RM" in errors[0]
        assert LIFT in errors[0]

    def test_violation_across_a_gap_is_still_caught(self, challenge):
        # Only 1RM and 5RM present (no 2RM-4RM); 5RM heavier than 1RM is a
        # violation even though they aren't numerically adjacent reps.
        targets = {LIFT: {1: Decimal("100"), 5: Decimal("150")}}
        errors = validate_rep_max_monotonicity(targets, challenge)
        assert len(errors) == 1
        assert "5RM" in errors[0] and "1RM" in errors[0]

    def test_only_first_violation_per_lift_reported(self, challenge):
        weights = {rep: Decimal("50") for rep in range(1, 11)}
        weights[3] = Decimal("200")
        weights[7] = Decimal("300")
        errors = validate_rep_max_monotonicity({LIFT: weights}, challenge)
        assert len(errors) == 1

    def test_missing_lift_has_no_errors(self, challenge):
        # Monotonicity has nothing to check against an empty table --
        # completeness (custom_goal_is_complete) is a separate concern.
        assert validate_rep_max_monotonicity({}, challenge) == []


class TestParseGrid:
    def test_grid_entry_parsed(self, challenge):
        targets, errors = parse_custom_goal_grid(grid_post(), challenge, "kg")
        assert errors == []
        assert set(targets[LIFT]) == set(range(1, 11))
        assert targets[LIFT][1] == Decimal("99.00")

    def test_grid_bad_value_reported(self, challenge):
        post = grid_post()
        post[grid_field_name(0, 1)] = "-3"
        _targets, errors = parse_custom_goal_grid(post, challenge, "kg")
        assert any("positive number" in e for e in errors)


class TestSaveCustomGoal:
    def test_creates_goal_and_points_participant_at_it(self, participant, challenge):
        _name, targets, _ = parse_custom_goal_json(full_json(), challenge, "kg")
        goal = save_custom_goal(participant, "My Goal", targets)
        participant.refresh_from_db()
        assert participant.custom_goal_id == goal.id
        assert CustomGoalTarget.objects.filter(goal=goal).count() == 10

    def test_second_save_raises_chart_is_locked(self, participant, challenge):
        # AC#4: charts are locked once a participant joins and saves a goal —
        # a second save_custom_goal call must not silently overwrite it.
        _name, targets, _ = parse_custom_goal_json(full_json(), challenge, "kg")
        goal = save_custom_goal(participant, "My Goal", targets)

        _name, new_targets, _ = parse_custom_goal_json(
            full_json(weights={str(rep): 200 for rep in range(1, 11)}),
            challenge,
            "kg",
        )
        with pytest.raises(ValueError):
            save_custom_goal(participant, "Renamed", new_targets)

        participant.refresh_from_db()
        assert participant.custom_goal_id == goal.id
        assert CustomGoal.objects.filter(participant=participant).count() == 1
        assert CustomGoalTarget.objects.filter(goal=goal).count() == 10
        assert CustomGoalTarget.objects.get(goal=goal, rep_count=1).target_weight == (
            Decimal("99.00")
        )


def test_grid_columns_render_10rm_to_1rm_left_to_right(user, challenge):
    # Matches the app-wide rep-max column convention (build_goal_setup_context
    # and the personal-data standards table both render 10RM..1RM left to
    # right); the custom-goal grid must not diverge from it.
    ctx = build_custom_goal_context(user, challenge)
    assert ctx["rep_range"] == list(range(10, 0, -1))
    assert [cell["rep"] for cell in ctx["lifts"][0]["cells"]] == list(range(10, 0, -1))


class TestCustomGoalFormBannerErrors:
    def test_json_missing_name_produces_banner_error(self, challenge):
        """A JSON submission with no "name" key reports a non-field error, since
        the JSON view has no separate name form field to attach it to."""
        form = CustomGoalForm(
            {"targets_json": full_json(name=None)},
            challenge=challenge,
            unit="kg",
            method=CustomGoal.SourceMethod.JSON,
        )
        assert not form.is_valid()
        banner_errors = form.banner_errors()
        assert any('"name"' in e for e in banner_errors)
        assert not any("Goal name:" in e for e in banner_errors)

    def test_grid_blank_name_defaults_instead_of_erroring(self, challenge):
        """A goal name is never demanded (TASK-248 plan §4): a blank name on
        the grid path silently falls back to a sensible per-method default,
        rather than reporting a field error."""
        form = CustomGoalForm(
            {"name": "", **grid_post()},
            challenge=challenge,
            unit="kg",
        )
        assert form.is_valid()
        assert form.name == "My Goal"
        assert not any("Goal name:" in e for e in form.banner_errors())

    def test_non_monotonic_grid_rejected(self, challenge):
        """A grid submission whose 5RM is heavier than its 4RM is rejected —
        rep-max monotonicity is enforced uniformly regardless of source."""
        weights = {rep: 100 - rep for rep in range(1, 11)}
        weights[5] = 500
        form = CustomGoalForm(
            {"name": "Spring", **grid_post(weights=weights)},
            challenge=challenge,
            unit="kg",
        )
        assert not form.is_valid()
        assert any("5RM" in e and "4RM" in e for e in form.banner_errors())

    def test_non_monotonic_json_rejected(self, challenge):
        weights = {str(rep): 100 - rep for rep in range(1, 11)}
        weights["5"] = 500
        form = CustomGoalForm(
            {"targets_json": full_json(weights=weights)},
            challenge=challenge,
            unit="kg",
            method=CustomGoal.SourceMethod.JSON,
        )
        assert not form.is_valid()
        assert any("5RM" in e and "4RM" in e for e in form.banner_errors())


class TestSetupView:
    def _url(self, challenge):
        return reverse("challenges:goal-setup", args=[challenge.pk])

    def _goto_chart_step(self, authed_client, challenge):
        """Select the "custom" (manual-entry) method, the wizard's
        precondition for the chart step this class's grid tests exercise
        directly (TASK-248: goal-setup is now a 3-step method -> inputs ->
        chart wizard)."""
        authed_client.post(self._url(challenge), {"method": "custom"})

    def _goto_json_step(self, authed_client, challenge):
        """Select the "json" method — its own top-level goal-setup method
        (TASK-306), not a toggle inside the manual-entry screen."""
        authed_client.post(self._url(challenge), {"method": "json"})

    def test_get_renders_grid(self, authed_client, participant, challenge):
        self._goto_chart_step(authed_client, challenge)
        response = authed_client.get(self._url(challenge))
        assert response.status_code == 200
        assert grid_field_name(0, 1).encode() in response.content

    def test_manual_entry_screen_has_no_json_panel(
        self, authed_client, participant, challenge
    ):
        self._goto_chart_step(authed_client, challenge)
        response = authed_client.get(self._url(challenge))
        content = response.content.decode()
        assert 'data-input-panel="grid"' in content
        assert 'data-input-panel="json"' not in content

    def test_json_screen_has_no_grid_panel(self, authed_client, participant, challenge):
        self._goto_json_step(authed_client, challenge)
        response = authed_client.get(self._url(challenge))
        content = response.content.decode()
        assert 'data-input-panel="json"' in content
        assert 'data-input-panel="grid"' not in content
        assert grid_field_name(0, 1).encode() not in response.content

    def test_page_includes_llm_prompt_with_lifts_and_schema(
        self, authed_client, participant, challenge
    ):
        self._goto_json_step(authed_client, challenge)
        response = authed_client.get(self._url(challenge))
        content = response.content.decode()
        assert "data-copy-prompt" in content
        assert "Copy AI prompt" in content
        # The prompt names this challenge's lift and embeds the schema/unit.
        assert LIFT in content
        assert "&quot;unit&quot;: &quot;kg&quot;" in content

    def test_json_error_rerenders_json_screen_with_value(
        self, authed_client, participant, challenge
    ):
        self._goto_json_step(authed_client, challenge)
        response = authed_client.post(
            self._url(challenge),
            {"targets_json": "{not valid json"},
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "valid JSON" in content
        assert 'data-input-panel="json"' in content
        assert "{not valid json" in content

    def test_post_saves_goal_and_activates_draft(
        self, authed_client, participant, challenge
    ):
        self._goto_json_step(authed_client, challenge)
        data = {"targets_json": full_json(name="Spring")}
        response = authed_client.post(self._url(challenge), data)
        assert response.status_code == 302
        participant.refresh_from_db()
        assert participant.custom_goal_id is not None
        assert participant.custom_goal.name == "Spring"
        assert participant.custom_goal.source_method == CustomGoal.SourceMethod.JSON
        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.ACTIVE

    def test_post_via_grid_saves_goal(self, authed_client, participant, challenge):
        self._goto_chart_step(authed_client, challenge)
        data = {"name": "Spring", **grid_post()}
        response = authed_client.post(self._url(challenge), data)
        assert response.status_code == 302
        participant.refresh_from_db()
        assert participant.custom_goal_id is not None

    def test_incomplete_submission_rerenders_with_errors_and_values(
        self, authed_client, participant, challenge
    ):
        self._goto_chart_step(authed_client, challenge)
        partial = {grid_field_name(0, 1): "80", grid_field_name(0, 2): "78"}
        response = authed_client.post(
            self._url(challenge), {"name": "Spring", **partial}
        )
        assert response.status_code == 200
        assert participant.custom_goal is None or participant.custom_goal_id is None
        content = response.content.decode()
        assert "is missing targets" in content
        # The two valid cells are preserved (display-formatted) for correction.
        assert 'value="80.0"' in content

    def test_grid_blank_name_defaults_and_saves(
        self, authed_client, participant, challenge
    ):
        """Empty name on a grid submission saves with the per-method default
        name rather than erroring (TASK-248 plan §4: a name is never
        demanded)."""
        self._goto_chart_step(authed_client, challenge)
        response = authed_client.post(
            self._url(challenge),
            {"name": "", **grid_post()},
        )
        assert response.status_code == 302
        participant.refresh_from_db()
        assert participant.custom_goal_id is not None
        assert participant.custom_goal.name == "My Goal"

    def test_json_missing_name_shows_banner_error(
        self, authed_client, participant, challenge
    ):
        """A JSON submission with no "name" key shows a banner error and saves
        nothing — the JSON view has no separate name form field."""
        self._goto_json_step(authed_client, challenge)
        response = authed_client.post(
            self._url(challenge),
            {"targets_json": full_json(name=None)},
        )
        assert response.status_code == 200
        participant.refresh_from_db()
        assert participant.custom_goal_id is None
        content = response.content.decode()
        assert "&quot;name&quot;" in content or '"name"' in content
        assert "Goal name:" not in content

    def test_completed_goal_redirects_away_from_setup(
        self, authed_client, participant, challenge
    ):
        _name, targets, _ = parse_custom_goal_json(full_json(), challenge, "kg")
        save_custom_goal(participant, "Done", targets)
        response = authed_client.get(self._url(challenge))
        assert response.status_code == 302
        assert f"/challenges/{challenge.pk}/" in response["Location"]


class TestChartLocking:
    """AC#4: charts cannot be edited after joining — the edit URL is gone
    entirely, and re-visiting goal-setup after a goal exists just redirects."""

    def test_custom_goal_edit_url_no_longer_resolves(self):
        placeholder_pk = "00000000-0000-0000-0000-000000000000"
        with pytest.raises(NoReverseMatch):
            reverse("challenges:custom-goal", args=[placeholder_pk])

    def test_setup_get_after_goal_configured_redirects_without_editing(
        self, authed_client, participant, challenge
    ):
        _name, targets, _ = parse_custom_goal_json(
            full_json(weights={str(rep): 123 for rep in range(1, 11)}),
            challenge,
            "kg",
        )
        save_custom_goal(participant, "Original", targets)
        setup_url = reverse("challenges:goal-setup", args=[challenge.pk])
        response = authed_client.get(setup_url)
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:detail", args=[challenge.pk])


BW_LIFT = "Chin-up"


@pytest.fixture
def bw_challenge(user):
    return make_custom_challenge(
        lifts=[BW_LIFT], creator=user, status=Challenge.Status.DRAFT
    )


@pytest.fixture
def bw_participant(bw_challenge, user):
    return ChallengeParticipantFactory(
        challenge=bw_challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )


class TestBodyweightAddedTargets:
    """Bodyweight-added lifts author targets as ADDED weight, so zero
    (bodyweight-only) and negative (machine-assisted) targets are valid (TASK-136).
    """

    def _bw_json(self, weights):
        return json.dumps({"name": "Goal", "unit": "kg", "targets": {BW_LIFT: weights}})

    def test_json_accepts_zero_and_negative(self, bw_challenge):
        weights = {"1": 20, "2": 15, "3": 10, "4": 5, "5": 0}
        weights.update({str(rep): -5 * (rep - 5) for rep in range(6, 11)})
        _name, targets, errors = parse_custom_goal_json(
            self._bw_json(weights), bw_challenge, "kg"
        )
        assert errors == []
        assert targets[BW_LIFT][5] == Decimal("0.00")
        assert targets[BW_LIFT][10] == Decimal("-25.00")

    def test_grid_accepts_zero_and_negative(self, bw_challenge):
        weights = {rep: 0 if rep == 5 else -5 for rep in range(1, 11)}
        post = {grid_field_name(0, rep): str(w) for rep, w in weights.items()}
        targets, errors = parse_custom_goal_grid(post, bw_challenge, "kg")
        assert errors == []
        assert targets[BW_LIFT][5] == Decimal("0.00")
        assert targets[BW_LIFT][1] == Decimal("-5.00")

    def test_json_non_numeric_reports_number_error(self, bw_challenge):
        _name, targets, errors = parse_custom_goal_json(
            self._bw_json({"1": "abc"}), bw_challenge, "kg"
        )
        assert any("must be a number." in e for e in errors)
        assert not any("positive number" in e for e in errors)
        assert 1 not in targets.get(BW_LIFT, {})

    def test_non_bodyweight_lift_still_rejects_non_positive(self, challenge):
        payload = json.dumps({"name": "Goal", "targets": {LIFT: {"1": 0, "2": -5}}})
        _name, _targets, errors = parse_custom_goal_json(payload, challenge, "kg")
        assert len([e for e in errors if "positive number" in e]) == 2

    def test_saving_zero_and_negative_targets_persists(
        self, bw_participant, bw_challenge
    ):
        weights = {str(rep): (rep - 5) for rep in range(1, 11)}
        _name, targets, errors = parse_custom_goal_json(
            self._bw_json(weights), bw_challenge, "kg"
        )
        assert errors == []
        assert custom_goal_is_complete(targets, bw_challenge) == []
        goal = save_custom_goal(bw_participant, "BW Goal", targets)
        assert CustomGoalTarget.objects.get(goal=goal, rep_count=5).target_weight == (
            Decimal("0.00")
        )
        assert CustomGoalTarget.objects.get(goal=goal, rep_count=1).target_weight == (
            Decimal("-4.00")
        )


class TestGoalSetupComputeLogView:
    """The manual-grid Compute button's fire-and-forget stats sink.

    Never mutates challenge state -- these tests only check the guard
    ladder (auth/participation) and that a valid payload reaches the
    logger with the expected structured fields, not any DB effect (there
    isn't one).
    """

    def _url(self, challenge):
        return reverse("challenges:goal-setup-compute-log", args=[challenge.pk])

    def test_anonymous_request_redirects_to_login(self, challenge):
        response = Client().post(
            self._url(challenge), data="[]", content_type="application/json"
        )
        assert response.status_code == 302

    def test_non_participant_is_forbidden(self, challenge):
        other_user = UserFactory(unit_preference="kg")
        client = Client()
        client.force_login(other_user)
        response = client.post(
            self._url(challenge), data="[]", content_type="application/json"
        )
        assert response.status_code == 403

    def test_valid_entry_is_logged_with_structured_fields(
        self, authed_client, challenge, participant
    ):
        entry = {
            "lift_name": LIFT,
            "target_rep": 1,
            "anchors": [
                {
                    "reps": 3,
                    "weight_kg": 100.0,
                    "formula_results_kg": {"epley": 110.0, "brzycki": 105.88},
                }
            ],
            "formula_spread_kg": 5.73,
            "anchor_spread_kg": 7.08,
            "blended_kg": 106.5234,
            "rounding_increment_kg": 2.5,
            "rounded_kg": 105.0,
        }
        with patch("challenges.views.logger") as mock_logger:
            response = authed_client.post(
                self._url(challenge),
                data=json.dumps([entry]),
                content_type="application/json",
            )
        assert response.status_code == 204
        mock_logger.info.assert_called_once_with(
            "Goal-setup compute run",
            extra={
                "challenge_id": challenge.pk,
                "lift_name": LIFT,
                "target_rep": 1,
                "anchors": entry["anchors"],
                "formula_spread_kg": 5.73,
                "anchor_spread_kg": 7.08,
                "blended_kg": 106.5234,
                "rounding_increment_kg": 2.5,
                "rounded_kg": 105.0,
            },
        )

    def test_multiple_entries_log_once_each(
        self, authed_client, challenge, participant
    ):
        entries = [
            {"lift_name": LIFT, "target_rep": 1},
            {"lift_name": LIFT, "target_rep": 10},
        ]
        with patch("challenges.views.logger") as mock_logger:
            response = authed_client.post(
                self._url(challenge),
                data=json.dumps(entries),
                content_type="application/json",
            )
        assert response.status_code == 204
        assert mock_logger.info.call_count == 2

    def test_malformed_json_body_is_discarded_not_500(
        self, authed_client, challenge, participant
    ):
        response = authed_client.post(
            self._url(challenge),
            data="not json",
            content_type="application/json",
        )
        assert response.status_code == 204

    def test_non_list_body_is_discarded_not_500(
        self, authed_client, challenge, participant
    ):
        response = authed_client.post(
            self._url(challenge),
            data=json.dumps({"not": "a list"}),
            content_type="application/json",
        )
        assert response.status_code == 204

    def test_non_dict_entries_are_skipped(self, authed_client, challenge, participant):
        with patch("challenges.views.logger") as mock_logger:
            response = authed_client.post(
                self._url(challenge),
                data=json.dumps(["not a dict", 42, None]),
                content_type="application/json",
            )
        assert response.status_code == 204
        mock_logger.info.assert_not_called()
