"""Tests for the goal-setup wizard (TASK-17, TASK-248, TASK-306).

Four methods — strength standards, suggested from history, manual entry,
JSON paste — all funnel through the same three-step session wizard
(method -> inputs -> chart) and materialise into the same CustomGoal/
CustomGoalTarget shape.
"""

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.custom_goals import grid_field_name
from challenges.models import Challenge, ChallengeParticipant, CustomGoal
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_custom_challenge,
)
from fitnessvolt.tests.factories import FitnessVoltStandardCacheFactory
from liftosaur.models import LiftSource
from liftosaur.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent


@pytest.fixture(autouse=True)
def _no_sync():
    """Goal-setup triggers a Liftosaur pull; stub it out so tests stay offline."""
    with patch("challenges.services.sync_user_lifts"):
        yield


@pytest.fixture
def user(db):
    return UserFactory(unit_preference="kg")


@pytest.fixture
def challenge(db, user):
    return make_custom_challenge(
        lifts=["Back Squat"], creator=user, status=Challenge.Status.DRAFT
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
    c = Client()
    c.force_login(user)
    return c


def _url(challenge):
    return reverse("challenges:goal-setup", args=[challenge.pk])


class TestAccess:
    def test_login_required(self, db, challenge):
        resp = Client().get(_url(challenge))
        assert resp.status_code == 302
        assert "/login" in resp.url or "next=" in resp.url

    def test_never_cached(self, authed_client, participant, challenge):
        """Every step shares this one URL -- without Cache-Control: no-store,
        a browser's history cache can restore a stale rendering of one step
        (e.g. the method step) in place of whatever the session says is
        actually current (UAT feedback: "Continue" after using Back sent the
        user back to the start of the wizard).
        """
        resp = authed_client.get(_url(challenge))
        assert "no-store" in resp.headers["Cache-Control"]

    def test_non_participant_forbidden(self, db, challenge):
        other = UserFactory()
        c = Client()
        c.force_login(other)
        resp = c.get(_url(challenge))
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_locked_challenge_redirects_without_scoring(
        self, authed_client, participant, challenge, status
    ):
        challenge.status = status
        challenge.save(update_fields=["status"])
        with patch("challenges.views.sync_and_score") as mock_score:
            resp = authed_client.get(_url(challenge))
        assert resp.status_code == 302
        assert resp.url == reverse("challenges:detail", args=[challenge.pk])
        mock_score.assert_not_called()

    def test_db_contention_during_sync_renders_step_with_warning(
        self, authed_client, participant, challenge
    ):
        """TASK-274: the reported 500 was OperationalError("database is locked")
        escaping this view's sync_and_score call. The wizard must still open
        against whatever is already pooled, with a warning — not a 500, and not
        a redirect back to this same URL (which risks a loop on the entry GET).
        """
        with patch(
            "challenges.views.sync_and_score",
            side_effect=OperationalError("database is locked"),
        ):
            resp = authed_client.get(_url(challenge))
        assert resp.status_code == 200
        assert b"Couldn&#x27;t refresh your Liftosaur history just now" in resp.content

    def test_redirects_with_message_when_goal_already_configured(
        self, authed_client, participant, challenge
    ):
        from challenges.custom_goals import save_custom_goal

        save_custom_goal(
            participant,
            "Done",
            {"Back Squat": {r: Decimal("100") for r in range(1, 11)}},
        )
        resp = authed_client.get(_url(challenge))
        assert resp.status_code == 302
        assert resp.url == f"/challenges/{challenge.pk}/"


class TestMethodStep:
    def test_standards_hidden_when_unavailable(
        self, authed_client, participant, challenge
    ):
        """No FITNESSVOLT_ENABLED, no warmed snapshot (the test-suite default):
        offering "standards" would dead-end at an empty population picker, so
        it must not be offered at all.
        """
        resp = authed_client.get(_url(challenge))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "standards" not in content
        assert "history" in content
        assert "custom" in content
        assert "json" in content
        assert "locked" in content.lower()

    def test_renders_four_methods_when_standards_available(
        self, authed_client, participant, challenge, settings
    ):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        resp = authed_client.get(_url(challenge))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "standards" in content
        assert "history" in content
        assert "custom" in content
        assert "json" in content
        assert "locked" in content.lower()

    def test_standards_post_rejected_when_unavailable(
        self, authed_client, participant, challenge
    ):
        """Defence in depth against a stale tab or tampered request posting
        method=standards after the flag was true but no longer is.
        """
        resp = authed_client.post(_url(challenge), {"method": "standards"})
        assert resp.status_code == 200  # re-renders the method step with errors
        assert (
            authed_client.post(_url(challenge), {"method": "history"}).status_code
            == 302
        )

    def test_invalid_method_rejected(self, authed_client, participant, challenge):
        resp = authed_client.post(_url(challenge), {"method": "bogus"})
        assert resp.status_code == 200  # re-renders the method step with errors

    def test_custom_method_skips_inputs_straight_to_chart(
        self, authed_client, participant, challenge
    ):
        resp = authed_client.post(_url(challenge), {"method": "custom"}, follow=False)
        assert resp.status_code == 302
        chart = authed_client.get(_url(challenge))
        assert b"Review your chart" in chart.content
        assert b"Back Squat" in chart.content


class TestWizardShellCentering:
    """Every goal-setup step renders inside base/wizard.html's centered column
    (TASK-281). Asserts on the `data-wizard-shell` hook rather than the Tailwind
    class string, except for the chart step's deliberately wider column."""

    def test_method_step_renders_the_centered_shell(
        self, authed_client, participant, challenge
    ):
        resp = authed_client.get(_url(challenge))
        assert b"data-wizard-shell" in resp.content
        assert b"mx-auto" in resp.content

    def test_chart_step_uses_the_wider_column(
        self, authed_client, participant, challenge
    ):
        """The 10-column rep grid needs more room than the default column, or
        it starts scrolling horizontally on desktop."""
        authed_client.post(_url(challenge), {"method": "custom"})
        resp = authed_client.get(_url(challenge))
        assert b"data-wizard-shell" in resp.content
        assert b"max-w-6xl" in resp.content

    def test_inputs_step_renders_the_centered_shell(
        self, authed_client, participant, challenge, user
    ):
        user.liftosaur_api_key = "existing-key"
        user.save(update_fields=["liftosaur_api_key"])
        authed_client.post(_url(challenge), {"method": "history"})
        resp = authed_client.get(_url(challenge))
        assert b"Stretch above your recent best" in resp.content
        assert b"data-wizard-shell" in resp.content

    def test_key_required_interstitial_renders_the_centered_shell(
        self, authed_client, participant, challenge
    ):
        """Reached mid-flow when the history method needs a Liftosaur key --
        it centers too, so the wizard doesn't visibly jump."""
        authed_client.post(_url(challenge), {"method": "history"})
        resp = authed_client.get(_url(challenge))
        assert "challenges/key_required.html" in [t.name for t in resp.templates]
        assert b"data-wizard-shell" in resp.content


class TestStaleWizardStepResubmission:
    """A browser's own Back button can show a cached render of an earlier
    step while the server-side session has already moved past it (every
    step posts to the same URL, so nothing in the URL itself distinguishes
    them). Resubmitting that stale page's form must not be parsed as the
    CURRENT step's own submission -- UAT feedback described exactly this as
    "weird state machine behaviour" after using Back.
    """

    def test_mismatched_wizard_step_is_dropped_not_misdispatched(
        self, authed_client, participant, challenge
    ):
        authed_client.post(_url(challenge), {"method": "custom"})
        # Session is now on "chart". Resubmit a stale cached "method" page
        # (its own wizard_step baked in at the time it was rendered).
        resp = authed_client.post(
            _url(challenge), {"method": "history", "wizard_step": "method"}
        )
        assert resp.status_code == 302
        # Not reprocessed as a method change -- still "custom", still chart.
        chart = authed_client.get(_url(challenge))
        assert b"Review your chart" in chart.content

    def test_matching_wizard_step_is_processed_normally(
        self, authed_client, participant, challenge
    ):
        resp = authed_client.post(
            _url(challenge), {"method": "custom", "wizard_step": "method"}
        )
        assert resp.status_code == 302
        chart = authed_client.get(_url(challenge))
        assert b"Review your chart" in chart.content

    def test_missing_wizard_step_still_works(
        self, authed_client, participant, challenge
    ):
        """Backward compatible with any request that never sends the field
        at all (scripts, or every other test in this file predating it)."""
        resp = authed_client.post(_url(challenge), {"method": "custom"})
        assert resp.status_code == 302

    def test_stale_resubmission_at_inputs_step_recovers_cleanly(
        self, authed_client, participant, challenge, settings
    ):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "standards", "wizard_step": "method"})
        # Session is now on "inputs". A stale cached "inputs" page from
        # BEFORE the user went back and changed their mind, still carrying
        # the OLD wizard_step (this is the same string here since inputs
        # doesn't change name across methods, so use a mismatched value
        # directly to prove the guard, not a step that happens to collide).
        resp = authed_client.post(
            url,
            {
                "bodyweight": "80",
                "sex": "M",
                "population": "verified",
                "tier": "Intermediate",
                "wizard_step": "method",
            },
        )
        assert resp.status_code == 302
        # Still on "inputs" -- the stale submission was dropped, not
        # misparsed as a method-step submission (which would have errored
        # on a missing "method" field) or accidentally advanced the wizard.
        still_inputs = authed_client.get(url)
        assert b"Sex" in still_inputs.content

    def test_continue_posted_to_a_back_1_url_does_not_regress_a_step(
        self, authed_client, participant, challenge, settings
    ):
        """TASK-264 UAT: "Continue" after "Back" occasionally reset all the
        way to the method step. goal_setup_inputs.html's <form> has no
        explicit action=, so a real browser submits it to the CURRENT
        document URL -- which, right after following the in-wizard "Back"
        link, still carries ?back=1. Simulate exactly that: GET the Back
        link, then POST Continue to that same ?back=1 URL, as a browser
        would. Without the request.method == "GET" guard, this silently
        double-decremented the session step index and tripped the
        wizard_step staleness guard into bouncing back to "method" instead
        of advancing to "chart".
        """
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "standards"})
        authed_client.post(
            url,
            {
                "bodyweight": "80",
                "sex": "M",
                "population": "verified",
                "tier": "Intermediate",
            },
        )
        # Now on chart (index 2); follow "Back" to inputs (index 1).
        back_resp = authed_client.get(url + "?back=1")
        assert b"Sex" in back_resp.content
        resp = authed_client.post(
            url + "?back=1",
            {
                "bodyweight": "82.5",
                "sex": "M",
                "population": "verified",
                "tier": "Intermediate",
            },
        )
        assert resp.status_code == 302
        assert resp.url == url
        chart = authed_client.get(url)
        assert b"Review your chart" in chart.content
        assert b"How do you want to set your goal" not in chart.content


class TestCustomMethodFullFlow:
    def test_confirm_creates_goal_via_grid_with_empty_source_detail(
        self, authed_client, participant, challenge
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "custom"})
        resp = authed_client.post(
            url,
            {
                "name": "My Custom Goal",
                **{grid_field_name(0, r): "100" for r in range(1, 11)},
            },
        )
        assert resp.status_code == 302
        assert resp.url == f"/challenges/{challenge.pk}/"

        participant.refresh_from_db()
        goal = participant.custom_goal
        assert goal is not None
        assert goal.source_method == CustomGoal.SourceMethod.CUSTOM
        assert goal.source_detail == {}
        assert goal.targets.count() == 10

        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.ACTIVE

    def test_cancel_clears_session_and_redirects_to_detail(
        self, authed_client, participant, challenge
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "custom"})
        resp = authed_client.get(url + "?cancel=1")
        assert resp.status_code == 302
        assert resp.url == reverse("challenges:detail", args=[challenge.pk])
        # Session cleared: revisiting starts over at the method step.
        restart = authed_client.get(url)
        assert b"How do you want to set your goal" in restart.content

    def test_back_returns_to_method_step(self, authed_client, participant, challenge):
        url = _url(challenge)
        authed_client.post(url, {"method": "custom"})
        resp = authed_client.get(url + "?back=1")
        assert b"How do you want to set your goal" in resp.content


class TestJsonMethodFullFlow:
    """JSON paste is its own top-level goal-setup method (TASK-306), not a
    toggle inside the manual-entry (CUSTOM) grid screen."""

    def test_confirm_creates_goal_with_empty_source_detail(
        self, authed_client, participant, challenge
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "json"})
        resp = authed_client.post(
            url,
            {
                "targets_json": json.dumps(
                    {
                        "name": "My JSON Goal",
                        "unit": "kg",
                        "targets": {"Back Squat": {str(r): 100 for r in range(1, 11)}},
                    }
                ),
            },
        )
        assert resp.status_code == 302
        assert resp.url == f"/challenges/{challenge.pk}/"

        participant.refresh_from_db()
        goal = participant.custom_goal
        assert goal is not None
        assert goal.name == "My JSON Goal"
        assert goal.source_method == CustomGoal.SourceMethod.JSON
        assert goal.source_detail == {}
        assert goal.targets.count() == 10

        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.ACTIVE

    def test_custom_method_skips_inputs_straight_to_chart(
        self, authed_client, participant, challenge
    ):
        resp = authed_client.post(_url(challenge), {"method": "json"}, follow=False)
        assert resp.status_code == 302
        chart = authed_client.get(_url(challenge))
        assert b"Paste JSON" in chart.content
        assert b"Back Squat" in chart.content


class TestHistoryMethodFlow:
    """These tests assume the user already has a connected Liftosaur key --
    the gate that gets one when they don't is covered separately in
    TestHistoryMethodRequiresLiftosaurKey."""

    @pytest.fixture(autouse=True)
    def _has_liftosaur_key(self, user):
        user.liftosaur_api_key = "existing-key"
        user.save(update_fields=["liftosaur_api_key"])

    def test_no_bodyweight_added_lift_still_shows_inputs_step_for_rounding(
        self, authed_client, participant, challenge
    ):
        """The challenge only covers Back Squat (not bodyweight-added), so no
        bodyweight is needed -- but the inputs step still appears, now that
        it also collects the rounding-increment choice needed for every
        history suggestion regardless of lift composition."""
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        resp = authed_client.get(url)
        assert b"Bodyweight" not in resp.content
        assert b"Stretch above your recent best" in resp.content
        assert b"Round targets to nearest" in resp.content

    def test_chart_step_prefills_name_from_uplift(
        self, authed_client, participant, challenge
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        authed_client.post(url, {"uplift_percent": "15", "rounding_increment": "none"})
        resp = authed_client.get(url)
        assert resp.context["goal_name"] == "Suggested from history (+15%)"
        assert b"Suggested from history (+15%)" in resp.content

    def test_custom_uplift_actually_changes_suggested_targets(
        self, authed_client, participant, challenge, user
    ):
        """The uplift field must not be decorative -- a different percentage
        must produce different suggested numbers, not just a different
        goal-name label (TASK-248 UAT feedback: make the 10% default
        editable)."""
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=timezone.now().date(),
            reps=1,
            weight_kg=Decimal("100.00"),
        )
        url = _url(challenge)

        authed_client.post(url, {"method": "history"})
        authed_client.post(url, {"uplift_percent": "0", "rounding_increment": "none"})
        no_stretch = authed_client.get(url)
        no_stretch_lift = next(
            lift for lift in no_stretch.context["lifts"] if lift["name"] == "Back Squat"
        )
        no_stretch_value = Decimal(
            str(next(c["value"] for c in no_stretch_lift["cells"] if c["value"]))
        )

        authed_client.get(url + "?cancel=1")
        authed_client.post(url, {"method": "history"})
        authed_client.post(url, {"uplift_percent": "50", "rounding_increment": "none"})
        big_stretch = authed_client.get(url)
        big_stretch_lift = next(
            lift
            for lift in big_stretch.context["lifts"]
            if lift["name"] == "Back Squat"
        )
        big_stretch_value = Decimal(
            str(next(c["value"] for c in big_stretch_lift["cells"] if c["value"]))
        )

        assert big_stretch_value > no_stretch_value * Decimal("1.4")

    def test_bodyweight_added_lift_also_shows_bodyweight_field(self, db):
        user = UserFactory(liftosaur_api_key="existing-key")
        challenge = make_custom_challenge(
            lifts=["Chin-up"], creator=user, status=Challenge.Status.DRAFT
        )
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        with patch("challenges.services.sync_user_lifts"):
            client.post(_url(challenge), {"method": "history"})
            resp = client.get(_url(challenge))
        assert b"Bodyweight" in resp.content
        assert b"Round targets to nearest" in resp.content

    def test_confirm_records_uplift_lookback_and_rounding_no_sex_or_bodyweight(
        self, authed_client, participant, challenge, user, settings
    ):
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=timezone.now().date(),
            reps=1,
            weight_kg=Decimal("100.00"),
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        # No bodyweight-added lift on this challenge, but inputs still runs
        # for the rounding choice.
        authed_client.post(url, {"rounding_increment": "kg:2.5"})
        resp = authed_client.get(url)
        assert resp.status_code == 200
        # A suggestion was prefilled for Back Squat from the LiftHistory row,
        # rounded to a clean 2.5 kg multiple.
        lift_ctx = next(
            lift for lift in resp.context["lifts"] if lift["name"] == "Back Squat"
        )
        assert any(cell["value"] for cell in lift_ctx["cells"])
        for cell in lift_ctx["cells"]:
            if cell["value"]:
                assert Decimal(str(cell["value"])) % Decimal("2.5") == 0

        grid_fields = {cell["field"]: cell["value"] for cell in lift_ctx["cells"]}
        resp = authed_client.post(url, {"name": "History Goal", **grid_fields})
        assert resp.status_code == 302

        participant.refresh_from_db()
        goal = participant.custom_goal
        assert goal.source_method == CustomGoal.SourceMethod.HISTORY
        assert set(goal.source_detail) == {
            "uplift",
            "lookback_days",
            "rounding_amount",
            "rounding_unit",
        }
        assert goal.source_detail["rounding_amount"] == "2.5"
        assert goal.source_detail["rounding_unit"] == "kg"
        assert (
            goal.source_detail["lookback_days"]
            == settings.CHALLENGES_GOAL_SUGGESTION_LOOKBACK_DAYS
        )

    def test_no_rounding_option_keeps_raw_precision(
        self, authed_client, participant, challenge, user
    ):
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=timezone.now().date(),
            reps=3,
            weight_kg=Decimal("101.00"),
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        authed_client.post(url, {"rounding_increment": "none"})
        resp = authed_client.get(url)
        lift_ctx = next(
            lift for lift in resp.context["lifts"] if lift["name"] == "Back Squat"
        )
        grid_fields = {cell["field"]: cell["value"] for cell in lift_ctx["cells"]}
        resp = authed_client.post(url, {"name": "History Goal", **grid_fields})
        assert resp.status_code == 302
        participant.refresh_from_db()
        assert participant.custom_goal.source_detail["rounding_amount"] is None

    def test_excludes_old_history_from_suggestion(
        self, authed_client, participant, challenge, user
    ):
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=(timezone.now() - timedelta(days=400)).date(),
            reps=1,
            weight_kg=Decimal("120.00"),
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        authed_client.post(url, {"rounding_increment": "kg:2.5"})
        resp = authed_client.get(url)
        lift_ctx = next(
            lift for lift in resp.context["lifts"] if lift["name"] == "Back Squat"
        )
        assert lift_ctx["needs_decision"] is True

    def test_needs_decision_row_uses_border_not_full_fill(
        self, authed_client, participant, challenge, user
    ):
        """UAT feedback: a full yellow bg-warning-light fill washed out the
        lift name in dark mode (warning.light is a static hex, not
        theme-aware). Border-only avoids touching text contrast at all. Also
        checks the reason text landed in its own row (colspan), not crammed
        into the narrow name cell alongside the rep-input columns.
        """
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=(timezone.now() - timedelta(days=400)).date(),
            reps=1,
            weight_kg=Decimal("120.00"),
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        authed_client.post(url, {"rounding_increment": "kg:2.5"})
        resp = authed_client.get(url)
        content = resp.content.decode()
        assert "bg-warning-light" not in content
        assert "border-warning" in content
        assert 'colspan="11"' in content

    def test_cancel_link_confirms_before_discarding(
        self, authed_client, participant, challenge
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "custom"})
        resp = authed_client.get(url)
        assert "confirm(" in resp.content.decode()


class TestHistoryMethodRequiresLiftosaurKey:
    """Joining a challenge never requires a Liftosaur key, and a challenge's
    creator never goes through a key gate at all (they're auto-added as a
    participant at creation) -- so without a gate here, picking "history"
    with neither a key nor any pooled history would silently produce an
    all-blank chart with no explanation why.
    """

    def test_prompts_for_key_instead_of_inputs_or_chart(
        self, authed_client, participant, challenge
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        resp = authed_client.get(url)
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "liftosaur_api_key" in content
        assert "Connect & Continue" in content
        assert "Review your chart" not in content

    def test_method_step_itself_is_never_gated(
        self, authed_client, participant, challenge
    ):
        resp = authed_client.get(_url(challenge))
        assert resp.status_code == 200
        assert "liftosaur_api_key" not in resp.content.decode()

    def test_other_methods_are_never_gated(self, authed_client, participant, challenge):
        resp = authed_client.post(_url(challenge), {"method": "custom"})
        assert resp.status_code == 302
        chart = authed_client.get(_url(challenge))
        assert b"Review your chart" in chart.content

    @patch("challenges.views.validate_liftosaur_key", return_value=False)
    def test_invalid_key_redisplays_prompt_with_error(
        self, mock_validate, authed_client, participant, challenge, user
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        resp = authed_client.post(url, {"liftosaur_api_key": "bad-key"})
        assert resp.status_code == 200
        assert b"Could not validate this Liftosaur API key." in resp.content
        user.refresh_from_db()
        assert user.liftosaur_api_key is None

    @patch("challenges.views.trigger_lift_history_backfill")
    @patch("challenges.views.validate_liftosaur_key", return_value=True)
    def test_valid_key_saves_and_proceeds(
        self,
        mock_validate,
        mock_backfill,
        authed_client,
        participant,
        challenge,
        user,
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        # Must NOT also call sync_and_score here: it would pull and pool the
        # same LiftHistory rows trigger_lift_history_backfill's own async
        # thread is already pooling -- redundant work, plus a second
        # concurrent writer that produced a real "database is locked" 500 in
        # UAT. (No explicit select_for_update was ever involved, despite what
        # this comment used to claim; the contention was one auto-committed
        # write per parsed set, batched away in TASK-274.)
        with patch("challenges.views.sync_and_score") as mock_sync:
            resp = authed_client.post(url, {"liftosaur_api_key": "brand-new-key"})
        mock_sync.assert_not_called()
        assert resp.status_code == 302
        mock_validate.assert_called_once_with("brand-new-key")
        user.refresh_from_db()
        assert user.liftosaur_api_key == "brand-new-key"
        mock_backfill.assert_called_once_with(user)

        # Inputs (rounding choice) always runs for history now, then chart.
        inputs = authed_client.get(url)
        assert b"Round targets to nearest" in inputs.content
        authed_client.post(url, {"rounding_increment": "kg:2.5"})
        chart = authed_client.get(url)
        assert b"Review your chart" in chart.content


class TestHistoryMethodRecognizesNonLiftosaurHistory:
    """A lifter with pooled history from a Hevy CSV import or manual
    self-report (LiftSource.HEVY / LiftSource.MANUAL) has real data to
    suggest from even with no Liftosaur key connected -- the key-required
    gate should only fire when there is genuinely nothing pooled yet."""

    def test_pooled_hevy_history_skips_the_key_prompt(
        self, authed_client, participant, challenge, user
    ):
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=timezone.now().date(),
            reps=1,
            weight_kg=Decimal("100.00"),
            source=LiftSource.HEVY,
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        resp = authed_client.get(url)
        assert resp.status_code == 200
        assert b"Round targets to nearest" in resp.content
        assert "liftosaur_api_key" not in resp.content.decode()

    def test_pooled_manual_history_skips_the_key_prompt(
        self, authed_client, participant, challenge, user
    ):
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=timezone.now().date(),
            reps=1,
            weight_kg=Decimal("100.00"),
            source=LiftSource.MANUAL,
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        resp = authed_client.get(url)
        assert resp.status_code == 200
        assert b"Round targets to nearest" in resp.content
        assert "liftosaur_api_key" not in resp.content.decode()

    def test_no_key_and_no_history_still_prompts(
        self, authed_client, participant, challenge
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        resp = authed_client.get(url)
        assert resp.status_code == 200
        assert "liftosaur_api_key" in resp.content.decode()


class TestStandardsMethodFlow:
    def test_inputs_step_renders_sex_population_tier(
        self, authed_client, participant, challenge, settings
    ):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "standards"})
        resp = authed_client.get(url)
        content = resp.content.decode()
        assert "Sex" in content
        assert "Population" in content
        assert "Tier" in content

    def test_chart_step_prefills_name_from_population_and_tier(
        self, authed_client, participant, challenge, settings
    ):
        """UAT feedback: the "Review your chart" page's name field showed up
        blank even though a sensible default was already computable --
        default_goal_name was only ever used as a submit-time fallback, never
        shown up front."""
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "standards"})
        authed_client.post(
            url,
            {
                "bodyweight": "80",
                "sex": "M",
                "population": "verified",
                "tier": "Intermediate",
            },
        )
        resp = authed_client.get(url)
        assert resp.context["goal_name"] == "Verified Intermediate"
        assert b'value="Verified Intermediate"' in resp.content

    def test_bodyweight_unit_defaults_to_user_preference(
        self, authed_client, participant, challenge, user, settings
    ):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        user.unit_preference = "lb"
        user.save(update_fields=["unit_preference"])
        url = _url(challenge)
        authed_client.post(url, {"method": "standards"})
        resp = authed_client.get(url)
        # No per-field unit override (UAT feedback: it could drift out of
        # sync with the account's own preference) -- bodyweight is always
        # interpreted in the account's unit_preference, shown as a plain
        # label next to the input, not a separate selectable dropdown.
        assert resp.context["form"].unit == "lb"
        assert b"In lb, matching your settings." in resp.content

    def test_back_to_inputs_step_preserves_bodyweight(
        self, authed_client, participant, challenge, settings
    ):
        """Bodyweight already given this session must survive a "Back" round
        trip (chart -> inputs), not silently reset to blank -- it's already
        stored in the wizard's session data, just not being re-shown.
        """
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "standards"})
        authed_client.post(
            url,
            {
                "bodyweight": "80",
                "sex": "M",
                "population": "verified",
                "tier": "Intermediate",
            },
        )
        # Now on the chart step; go back to inputs.
        resp = authed_client.get(url + "?back=1")
        assert resp.context["form"]["bodyweight"].value() == Decimal("80.0")

    def test_confirm_records_full_provenance(
        self, authed_client, participant, challenge, settings
    ):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "standards"})
        resp = authed_client.post(
            url,
            {
                "bodyweight": "80",
                "sex": "M",
                "population": "verified",
                "tier": "Intermediate",
            },
        )
        assert resp.status_code == 302

        chart = authed_client.get(url)
        assert chart.status_code == 200
        assert "Paste JSON" not in chart.content.decode()
        lift_ctx = next(
            lift for lift in chart.context["lifts"] if lift["name"] == "Back Squat"
        )
        assert any(cell["value"] for cell in lift_ctx["cells"])

        # Confirm via the grid (the only path standards offers) — not JSON,
        # which is CUSTOM-only precisely so a submission can't diverge from
        # what source_detail below claims produced it.
        grid_fields = {cell["field"]: cell["value"] for cell in lift_ctx["cells"]}
        resp = authed_client.post(url, {"name": "Standards Goal", **grid_fields})
        assert resp.status_code == 302

        participant.refresh_from_db()
        goal = participant.custom_goal
        assert goal.source_method == CustomGoal.SourceMethod.STANDARDS
        assert goal.source_detail == {
            "population": "verified",
            "snapshot_version": "2026-06-09",
            "tier": "Intermediate",
            "sex": "M",
            "bodyweight_kg": "80.00",
            "rounding_amount": "2.5",
            "rounding_unit": "kg",
        }
        # JSON-serialisable: the decimal string round-trips cleanly.
        assert isinstance(goal.source_detail["bodyweight_kg"], str)

    def test_targets_json_ignored_for_standards_method(
        self, authed_client, participant, challenge, settings
    ):
        """A targets_json payload must never win for a non-JSON method —
        otherwise the saved targets could diverge from what source_detail
        claims produced them (the UI no longer offers this; this is the
        server-side backstop against a stale tab or a hand-crafted POST).
        """
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "standards"})
        authed_client.post(
            url,
            {
                "bodyweight": "80",
                "sex": "M",
                "population": "verified",
                "tier": "Intermediate",
            },
        )
        chart = authed_client.get(url)
        lift_ctx = next(
            lift for lift in chart.context["lifts"] if lift["name"] == "Back Squat"
        )
        grid_fields = {cell["field"]: cell["value"] for cell in lift_ctx["cells"]}

        resp = authed_client.post(
            url,
            {
                "name": "Standards Goal",
                **grid_fields,
                "targets_json": json.dumps(
                    {
                        "name": "Sneaky JSON Goal",
                        "unit": "kg",
                        "targets": {"Back Squat": {str(r): 999 for r in range(1, 11)}},
                    }
                ),
            },
        )
        assert resp.status_code == 302

        participant.refresh_from_db()
        goal = participant.custom_goal
        assert goal.name == "Standards Goal"
        assert goal.source_method == CustomGoal.SourceMethod.STANDARDS
        assert all(
            target.target_weight != Decimal("999") for target in goal.targets.all()
        )

    def test_invalid_sex_rejected(
        self, authed_client, participant, challenge, settings
    ):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "standards"})
        resp = authed_client.post(
            url,
            {
                "bodyweight": "80",
                "sex": "",
                "population": "verified",
                "tier": "Intermediate",
            },
        )
        assert resp.status_code == 200  # re-rendered with errors, not advanced

    def test_missing_bodyweight_rejected(
        self, authed_client, participant, challenge, settings
    ):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version="2026-06-09",
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "standards"})
        resp = authed_client.post(
            url,
            {
                "bodyweight": "",
                "sex": "M",
                "population": "verified",
                "tier": "Intermediate",
            },
        )
        assert resp.status_code == 200


class TestBackfillScoring:
    """Confirming the chart step scores the already-pooled LiftHistory
    immediately, so the leaderboard reflects the new goal without waiting for
    the next sync (mirrors TASK-95's original guarantee)."""

    def test_confirm_backfills_point_events(
        self, authed_client, participant, challenge, user
    ):
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=timezone.now().date(),
            reps=1,
            weight_kg=Decimal("120.00"),
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "json"})
        resp = authed_client.post(
            url,
            {
                "targets_json": json.dumps(
                    {
                        "name": "Goal",
                        "unit": "kg",
                        "targets": {"Back Squat": {str(r): 100 for r in range(1, 11)}},
                    }
                ),
            },
        )
        assert resp.status_code == 302
        assert PointEarnEvent.objects.filter(
            user=user,
            challenge=challenge,
            lift="Back Squat",
            is_current_best=True,
            points_earned__gt=0,
        ).exists()
