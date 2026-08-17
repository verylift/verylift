"""Provenance-boundary tests for the goal-setup wizard (TASK-248 plan step 67).

The second silent-wrong-answer risk this task carries: bodyweight/sex must
flow through the wizard as pure ephemeral inputs, never landing on ``User``,
never surviving in the session past goal completion, and never persisted
anywhere except ``CustomGoal.source_detail`` for the STANDARDS method. A bug
here doesn't crash -- it quietly resurrects exactly the per-user bodyweight
store this task set out to delete, or leaks it into HISTORY/CUSTOM goals that
must never carry it.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.apps import apps
from django.db import connection
from django.test import Client
from django.urls import reverse

from accounts.models import User
from accounts.tests.factories import UserFactory
from challenges.custom_goals import grid_field_name
from challenges.models import Challenge, ChallengeParticipant, CustomGoal
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_custom_challenge,
)
from fitnessvolt.tests.factories import FitnessVoltStandardCacheFactory
from liftosaur.tests.factories import LiftHistoryFactory

pytestmark = pytest.mark.django_db

SNAPSHOT_VERSION = "2026-06-09"


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


def _user_row(user):
    return dict(User.objects.values().get(pk=user.pk))


def _confirm_targets(authed_client, url, name, lift):
    """Confirm the chart step via the grid -- not JSON, which is CUSTOM-only
    (a non-CUSTOM submission ignores targets_json; see test_goal_setup_view's
    provenance-mismatch regression test). Every fixture challenge here has
    exactly one configured lift, always at grid index 0."""
    del lift  # kept for call-site readability; grid position is always 0
    fields = {grid_field_name(0, rep): "100" for rep in range(1, 11)}
    return authed_client.post(url, {"name": name, **fields})


class TestNoUserFieldChanged:
    """No goal-setup method may write anything onto ``User`` -- there is no
    sex or bodyweight column left to write to, so a regression here would
    have to resurrect one, not just mis-set an existing value."""

    def test_custom_method_leaves_user_row_untouched(
        self, authed_client, participant, challenge, user
    ):
        before = _user_row(user)
        url = _url(challenge)
        authed_client.post(url, {"method": "custom"})
        resp = _confirm_targets(authed_client, url, "Custom Goal", "Back Squat")
        assert resp.status_code == 302
        assert _user_row(user) == before

    def test_history_method_leaves_user_row_untouched(
        self, authed_client, participant, challenge, user
    ):
        # Already-connected key: the history-method key gate (TASK-248 UAT
        # feedback) is a different concern, covered in
        # TestHistoryMethodRequiresLiftosaurKey.
        user.liftosaur_api_key = "existing-key"
        user.save(update_fields=["liftosaur_api_key"])
        LiftHistoryFactory(
            user=user, lift="Back Squat", reps=1, weight_kg=Decimal("100.00")
        )
        before = _user_row(user)
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        # Inputs (rounding choice) always runs for history now.
        authed_client.post(url, {"rounding_increment": "kg:2.5"})
        resp = _confirm_targets(authed_client, url, "History Goal", "Back Squat")
        assert resp.status_code == 302
        assert _user_row(user) == before

    def test_standards_method_leaves_user_row_untouched(
        self, authed_client, participant, challenge, user, settings
    ):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version=SNAPSHOT_VERSION,
        )
        before = _user_row(user)
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
        resp = _confirm_targets(authed_client, url, "Standards Goal", "Back Squat")
        assert resp.status_code == 302
        # The submitted sex/bodyweight never touch User -- the row is
        # bit-for-bit identical to before the wizard ran.
        assert _user_row(user) == before


class TestStandardsProvenanceExact:
    def test_source_detail_matches_submitted_sex_and_bodyweight_exactly(
        self, authed_client, participant, challenge, settings
    ):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version=SNAPSHOT_VERSION,
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
        resp = _confirm_targets(authed_client, url, "Standards Goal", "Back Squat")
        assert resp.status_code == 302

        participant.refresh_from_db()
        goal = participant.custom_goal
        assert goal.source_method == CustomGoal.SourceMethod.STANDARDS
        assert goal.source_detail == {
            "population": "verified",
            "snapshot_version": SNAPSHOT_VERSION,
            "tier": "Intermediate",
            "sex": "M",
            "bodyweight_kg": "80.00",
            "rounding_amount": "2.5",
            "rounding_unit": "kg",
        }


class TestHistoryAndCustomCarryNoSexOrBodyweight:
    def test_history_source_detail_has_no_sex_or_bodyweight_keys(
        self, authed_client, participant, challenge, user
    ):
        user.liftosaur_api_key = "existing-key"
        user.save(update_fields=["liftosaur_api_key"])
        LiftHistoryFactory(
            user=user, lift="Back Squat", reps=1, weight_kg=Decimal("100.00")
        )
        url = _url(challenge)
        authed_client.post(url, {"method": "history"})
        # Inputs (rounding choice) always runs for history now.
        authed_client.post(url, {"rounding_increment": "kg:2.5"})
        resp = _confirm_targets(authed_client, url, "History Goal", "Back Squat")
        assert resp.status_code == 302

        participant.refresh_from_db()
        goal = participant.custom_goal
        assert set(goal.source_detail) == {
            "uplift",
            "lookback_days",
            "rounding_amount",
            "rounding_unit",
        }
        assert "sex" not in goal.source_detail
        assert "bodyweight_kg" not in goal.source_detail

    def test_custom_source_detail_is_empty(self, authed_client, participant, challenge):
        url = _url(challenge)
        authed_client.post(url, {"method": "custom"})
        resp = _confirm_targets(authed_client, url, "Custom Goal", "Back Squat")
        assert resp.status_code == 302

        participant.refresh_from_db()
        assert participant.custom_goal.source_detail == {}


class TestSessionClearedAfterCompletion:
    def test_session_has_no_goal_setup_keys_after_confirming(
        self, authed_client, participant, challenge
    ):
        url = _url(challenge)
        authed_client.post(url, {"method": "custom"})
        # Mid-wizard: the session carries this challenge's draft under a
        # pk-namespaced key.
        session = authed_client.session
        # SessionBase doesn't implement plain iteration (Django-specific
        # quirk -- unlike a real dict, ``for key in session`` breaks), so
        # .keys() is required here, not the usual dict-membership idiom.
        assert any("goal_setup" in key for key in session.keys())  # noqa: SIM118

        resp = _confirm_targets(authed_client, url, "Custom Goal", "Back Squat")
        assert resp.status_code == 302

        session = authed_client.session
        for key, value in session.items():
            if "goal_setup" in key:
                # The per-challenge namespaced dict must no longer carry this
                # challenge's draft; an empty dict (all challenges cleaned up)
                # is fine, a stale entry is not.
                assert str(challenge.pk) not in value

    def test_cancel_also_clears_session(self, authed_client, participant, challenge):
        url = _url(challenge)
        authed_client.post(url, {"method": "custom"})
        authed_client.get(url + "?cancel=1")
        session = authed_client.session
        for key, value in session.items():
            if "goal_setup" in key:
                assert str(challenge.pk) not in value


class TestNoBodyweightPersistenceOutsideProvenance:
    """Static, whole-codebase tripwires: bodyweight/sex must not resurface as
    a column anywhere else, ever -- not just "not in the tests we happened to
    write". A regression here is architectural, not a single wrong value."""

    # The only two model fields in the entire codebase allowed to mention
    # "bodyweight" or "sex": a static exercise-classification boolean (not a
    # bodyweight value) and a strength-standards *population cohort* label
    # (not a specific user's sex). Both are reference data, never personal.
    _ALLOWED_REFERENCE_FIELDS = {
        "liftosaur.Lift.is_bodyweight_added",
        "fitnessvolt.FitnessVoltStandardCache.sex",
    }

    def test_no_model_field_mentions_bodyweight_or_sex_except_known_reference_data(
        self,
    ):
        offending = set()
        for model in apps.get_models():
            for field in model._meta.get_fields():
                name = getattr(field, "name", "") or ""
                lname = name.lower()
                if "bodyweight" in lname or lname == "sex":
                    offending.add(f"{model._meta.label}.{name}")
        assert offending == self._ALLOWED_REFERENCE_FIELDS

    def test_no_bodyweightlog_shaped_table_exists(self):
        table_names = {name.lower() for name in connection.introspection.table_names()}
        assert not any("bodyweightlog" in name for name in table_names)

    def test_user_table_has_no_sex_or_bodyweight_column(self):
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, User._meta.db_table
            )
        column_names = {c.name.lower() for c in columns}
        assert "sex" not in column_names
        assert not any("bodyweight" in name for name in column_names)

    def test_customgoal_source_detail_is_the_only_json_field_on_the_model(self):
        # CustomGoal.source_detail is the sole writer of a bodyweight/sex
        # value anywhere (TASK-248 plan §4); confirm the model has exactly
        # one JSONField, so there is no sibling field a future change could
        # accidentally also start writing bodyweight into.
        json_fields = [
            f.name
            for f in CustomGoal._meta.get_fields()
            if f.__class__.__name__ == "JSONField"
        ]
        assert json_fields == ["source_detail"]
