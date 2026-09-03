"""Tests for the restamp_lb_converted_lift_history repair command (TASK-327)."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command

from accounts.tests.factories import UserFactory
from challenges.custom_goals import save_custom_goal
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_custom_challenge,
)
from core.models import LiftHistory, LiftSource
from core.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent

PERFORMED_AT = date(2025, 6, 20)
OLD_WEIGHT_KG = Decimal("102.28")  # 225.5 lb under the old truncated factor
NEW_WEIGHT_KG = Decimal("102.29")  # the same 225.5 lb under the exact factor


def _setup(*, status=Challenge.Status.ACTIVE):
    """Challenge covering Back Squat, with one participant with a locked goal."""
    user = UserFactory()
    challenge = make_custom_challenge(
        lifts=["Back Squat"],
        status=status,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 12, 31),
    )
    participant = ChallengeParticipantFactory(
        user=user,
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )
    save_custom_goal(
        participant,
        "Goal",
        {"Back Squat": {rep: Decimal("10.00") for rep in range(1, 11)}},
    )
    return user, challenge


def _stale_row(user, source=LiftSource.HEVY_CSV, weight_kg=OLD_WEIGHT_KG):
    return LiftHistoryFactory(
        user=user,
        lift="Back Squat",
        performed_at=PERFORMED_AT,
        reps=5,
        weight_kg=weight_kg,
        source=source,
    )


@pytest.mark.django_db
class TestRestampLbConvertedLiftHistory:
    def test_affected_row_from_a_candidate_source_is_restamped_and_scored(self):
        user, challenge = _setup()
        _stale_row(user)

        call_command("restamp_lb_converted_lift_history")

        row = LiftHistory.objects.get(user=user)
        assert row.weight_kg == NEW_WEIGHT_KG
        assert PointEarnEvent.objects.filter(
            user=user, challenge=challenge, lift="Back Squat"
        ).exists()

    def test_reimport_of_the_same_physical_set_ends_as_one_row_not_two(self):
        """TASK-327 acceptance criterion #4.

        The stale row simulates a set imported before TASK-325 landed. The
        second row simulates the same physical set arriving again through a
        source that now runs the exact factor (a re-import, or a live Hevy
        API sync of a set previously pulled from Hevy's CSV export). Before
        this repair, the two rows would sit 0.01 kg apart forever and double
        the lifter's pooled volume for one real set.
        """
        user, _ = _setup()
        _stale_row(user)
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=PERFORMED_AT,
            reps=5,
            weight_kg=NEW_WEIGHT_KG,
            source=LiftSource.HEVY,
        )

        call_command("restamp_lb_converted_lift_history")

        rows = LiftHistory.objects.filter(user=user)
        assert rows.count() == 1
        assert rows.get().weight_kg == NEW_WEIGHT_KG

    def test_source_never_run_through_lb_to_kg_is_left_untouched(self):
        # HEVY (the live API sync, TASK-332) takes weight_kg directly from
        # Hevy's API -- it never runs a conversion, so this value landing on
        # the affected grid is coincidence, not evidence of a stale
        # conversion.
        user, _ = _setup()
        row = _stale_row(user, source=LiftSource.HEVY)

        call_command("restamp_lb_converted_lift_history")

        assert LiftHistory.objects.get(pk=row.pk).weight_kg == OLD_WEIGHT_KG

    def test_liftosaur_csv_import_is_a_candidate(self):
        """TASK-332: LIFTOSAUR_CSV (the CSV importer's now-distinct source)
        converts lb the same way LIFTOSAUR (the live sync) does, so it must
        stay in _CANDIDATE_SOURCES even though it didn't exist as a separate
        value before this task."""
        user, challenge = _setup()
        row = _stale_row(user, source=LiftSource.LIFTOSAUR_CSV)

        call_command("restamp_lb_converted_lift_history")

        assert LiftHistory.objects.get(pk=row.pk).weight_kg == NEW_WEIGHT_KG
        assert PointEarnEvent.objects.filter(
            user=user, challenge=challenge, lift="Back Squat"
        ).exists()

    def test_manual_source_is_left_untouched(self):
        user, _ = _setup()
        row = _stale_row(user, source=LiftSource.MANUAL)

        call_command("restamp_lb_converted_lift_history")

        assert LiftHistory.objects.get(pk=row.pk).weight_kg == OLD_WEIGHT_KG

    def test_unaffected_value_from_a_candidate_source_is_untouched(self):
        user, _ = _setup()
        # 100 lb rounds to 45.36 kg under both the old and exact factor.
        row = _stale_row(user, weight_kg=Decimal("45.36"))

        call_command("restamp_lb_converted_lift_history")

        assert LiftHistory.objects.get(pk=row.pk).weight_kg == Decimal("45.36")

    def test_dry_run_makes_no_changes(self):
        user, challenge = _setup()
        _stale_row(user)

        call_command("restamp_lb_converted_lift_history", "--dry-run")

        assert LiftHistory.objects.get(user=user).weight_kg == OLD_WEIGHT_KG
        assert not PointEarnEvent.objects.filter(challenge=challenge).exists()

    def test_frozen_challenge_is_not_rescored(self):
        user, challenge = _setup(status=Challenge.Status.COMPLETED)
        _stale_row(user)

        call_command("restamp_lb_converted_lift_history")

        # Row is still restamped, but the frozen ledger is not written.
        assert LiftHistory.objects.get(user=user).weight_kg == NEW_WEIGHT_KG
        assert not PointEarnEvent.objects.filter(challenge=challenge).exists()

    def test_username_filter_limits_repair(self):
        user, _ = _setup()
        _stale_row(user)
        other_user, _ = _setup()
        _stale_row(other_user)

        call_command("restamp_lb_converted_lift_history", "--username", user.username)

        assert LiftHistory.objects.get(user=user).weight_kg == NEW_WEIGHT_KG
        assert LiftHistory.objects.get(user=other_user).weight_kg == OLD_WEIGHT_KG
