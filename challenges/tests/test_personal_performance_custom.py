"""Personal-performance detail data for CUSTOM challenges (TASK-134, TASK-248)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.services import build_personal_data
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    CustomGoalFactory,
    CustomGoalTargetFactory,
    make_custom_challenge,
)
from scoring.tests.factories import PointEarnEventFactory

pytestmark = pytest.mark.django_db

# The documented defaults, pinned in tests via the ``settings`` fixture so they
# stay deterministic regardless of any ambient CHALLENGES_CLOSE_TO_GOAL_*
# override in the runtime .env.
CLOSE_TO_GOAL_GAP_FRACTION = Decimal("0.05")
CLOSE_TO_GOAL_REPS_GAP = 2

ENDGAME_WINDOW_DAYS = 14
ENDGAME_GAP_FRACTION = Decimal("0.05")
ENDGAME_REPS_GAP = 2

LIFT = "Bench Press"


@pytest.fixture
def user():
    return UserFactory(unit_preference="kg")


@pytest.fixture
def challenge(user):
    return make_custom_challenge(
        lifts=[LIFT], creator=user, status=Challenge.Status.ACTIVE
    )


@pytest.fixture
def participant(user, challenge):
    participant = ChallengeParticipantFactory(
        user=user,
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        joined_at=timezone.now() - timedelta(days=30),
    )
    goal = CustomGoalFactory(participant=participant, name="Spring Targets")
    for rep in range(1, 11):
        CustomGoalTargetFactory(
            goal=goal, lift=LIFT, rep_count=rep, target_weight=Decimal("100.00")
        )
    participant.custom_goal = goal
    participant.save(update_fields=["custom_goal"])
    return participant


def test_returns_none_without_a_custom_goal(user, challenge):
    participant = ChallengeParticipantFactory(
        user=user,
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        joined_at=timezone.now() - timedelta(days=30),
    )
    assert build_personal_data(user, challenge, participant) is None


def test_standards_rows_read_flat_targets(user, challenge, participant):
    data = build_personal_data(user, challenge, participant)
    assert data is not None
    assert data["goal_label"] == "Spring Targets"
    row = next(r for r in data["standards_rows"] if r["lift"] == LIFT)
    # Every rep-max cell shows the flat 100 kg target (no Epley taper).
    assert {cell["weight"] for cell in row["cells"]} == {Decimal("100.0")}


def test_standards_rows_are_not_plate_snapped(user, challenge, participant):
    # Challenge.smallest_plate defaults to 1.25 kg (a 2.5 kg loadable
    # increment), which is exactly the built-in/FitnessVolt display-rounding
    # grid — a hand-entered custom target must NOT be snapped to it.
    goal = participant.custom_goal
    goal.targets.filter(rep_count=5).update(target_weight=Decimal("102.30"))
    data = build_personal_data(user, challenge, participant)
    row = next(r for r in data["standards_rows"] if r["lift"] == LIFT)
    cell = next(c for c in row["cells"] if c["reps"] == 5)
    assert cell["weight"] == Decimal("102.3")


def test_gap_card_weight_gap_is_not_plate_snapped(user, challenge, participant):
    goal = participant.custom_goal
    goal.targets.filter(rep_count=10).update(target_weight=Decimal("102.30"))
    PointEarnEventFactory(
        user=user,
        challenge=challenge,
        lift=LIFT,
        performed_at=timezone.now().date(),
        reps=10,
        weight=Decimal("50.00"),
        points_earned=0,
        is_current_best=False,
    )
    data = build_personal_data(user, challenge, participant)
    card = next(c for c in data["summary_cards"] if c["lift"] == LIFT)
    # Gap to the 10RM floor (102.3 kg) from a 50 kg set, measured exactly with
    # no fuzz band: 102.3 - 50 = 52.3, not plate-snapped to a loadable value.
    assert card["weight_gap"] == Decimal("52.3")


def test_close_to_goal_gap_fraction_is_computed_pre_snap_in_kg(
    user, challenge, participant, settings
):
    settings.CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION = float(CLOSE_TO_GOAL_GAP_FRACTION)
    settings.CHALLENGES_CLOSE_TO_GOAL_REPS_GAP = CLOSE_TO_GOAL_REPS_GAP
    # A hand-entered 10RM target of 102.30 kg (not a plate-loadable value) with a
    # 97.50 kg set: the raw kg gap is exactly 4.80, so gap_fraction is 4.80/102.30
    # (~0.0469, inside the 5% band). Proving the fraction equals this exact ratio
    # shows it is taken from the pre-snap kg gap, not a plate-snapped display gap.
    goal = participant.custom_goal
    goal.targets.filter(rep_count=10).update(target_weight=Decimal("102.30"))
    PointEarnEventFactory(
        user=user,
        challenge=challenge,
        lift=LIFT,
        performed_at=timezone.now().date(),
        reps=10,
        weight=Decimal("97.50"),
        points_earned=0,
        is_current_best=False,
    )
    data = build_personal_data(user, challenge, participant)
    card = next(c for c in data["summary_cards"] if c["lift"] == LIFT)
    assert card["state"] == "no_points"
    assert card["gap_fraction"] == Decimal("4.80") / Decimal("102.30")
    assert card["gap_fraction"] <= CLOSE_TO_GOAL_GAP_FRACTION
    assert card["close_to_goal"] is True


def test_scored_card_shows_goal_name_not_sentinel(user, challenge, participant):
    PointEarnEventFactory(
        user=user,
        challenge=challenge,
        lift=LIFT,
        performed_at=timezone.now().date(),
        reps=1,
        weight=Decimal("100.00"),
        points_earned=10,
        is_current_best=True,
    )
    data = build_personal_data(user, challenge, participant)
    card = next(c for c in data["summary_cards"] if c["lift"] == LIFT)
    assert card["state"] == "scored"
    assert card["tier_satisfied"] == "Spring Targets"


def test_scored_lift_gets_endgame_next_point_suggestion(
    user, challenge, participant, settings
):
    # AC #8/#9: CUSTOM has full point-tier math (flat targets), just no tier
    # name. A scored lift in the endgame window gets a next-point suggestion
    # whose weight gap is not plate-snapped (custom targets are hand-entered).
    settings.CHALLENGES_ENDGAME_WINDOW_DAYS = ENDGAME_WINDOW_DAYS
    settings.CHALLENGES_ENDGAME_GAP_FRACTION = float(ENDGAME_GAP_FRACTION)
    settings.CHALLENGES_ENDGAME_REPS_GAP = ENDGAME_REPS_GAP
    challenge.end_date = date.today() + timedelta(days=3)
    challenge.save(update_fields=["end_date"])
    # A 5-point best sits at reps = 11 - 5 = 6. The next point (6) targets
    # reps = 5, so raise just that rep target above the scored weight.
    goal = participant.custom_goal
    goal.targets.filter(rep_count=5).update(target_weight=Decimal("101.00"))
    PointEarnEventFactory(
        user=user,
        challenge=challenge,
        lift=LIFT,
        performed_at=timezone.now().date(),
        reps=6,
        weight=Decimal("100.00"),
        points_earned=5,
        is_current_best=True,
    )
    data = build_personal_data(user, challenge, participant)
    card = next(c for c in data["summary_cards"] if c["lift"] == LIFT)
    assert card["state"] == "scored"
    assert card["next_point_gap_fraction"] == Decimal("1.00") / Decimal("101.00")
    assert card["endgame_suggestion"] == "next_point"
    assert card["next_point_weight_gap"] is not None


BW_LIFT = "Chin-up"


@pytest.fixture
def bw_challenge(user):
    return make_custom_challenge(
        lifts=[BW_LIFT], creator=user, status=Challenge.Status.ACTIVE
    )


def _bw_participant(user, challenge, targets):
    participant = ChallengeParticipantFactory(
        user=user,
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        joined_at=timezone.now() - timedelta(days=30),
    )
    goal = CustomGoalFactory(participant=participant, name="BW Targets")
    for rep, weight in targets.items():
        CustomGoalTargetFactory(
            goal=goal, lift=BW_LIFT, rep_count=rep, target_weight=weight
        )
    participant.custom_goal = goal
    participant.save(update_fields=["custom_goal"])
    return participant


class TestBodyweightAddedCustomDisplay:
    """Custom bodyweight-added targets are authored as added weight, and (as of
    TASK-248) the stored/displayed number IS the added weight directly — no
    bodyweight arithmetic, and so no dependency on a bodyweight ever existing.
    """

    def test_standards_cells_show_authored_added_value(self, user, bw_challenge):
        targets = {rep: Decimal("0.00") for rep in range(1, 11)}
        targets[1] = Decimal("5.00")
        targets[2] = Decimal("-10.00")
        participant = _bw_participant(user, bw_challenge, targets)
        data = build_personal_data(user, bw_challenge, participant)
        row = next(r for r in data["standards_rows"] if r["lift"] == BW_LIFT)
        by_reps = {c["reps"]: c["weight"] for c in row["cells"]}
        assert by_reps[1] == "+5"
        assert by_reps[2] == "-10"
        assert by_reps[3] == "BW"

    def test_assisted_shows_negative(self, user, bw_challenge):
        # Band-assisted work stays legal and visible as a negative added
        # weight — only leverage-machine equipment is excluded from scoring.
        targets = {rep: Decimal("-15.00") for rep in range(1, 11)}
        participant = _bw_participant(user, bw_challenge, targets)
        data = build_personal_data(user, bw_challenge, participant)
        row = next(r for r in data["standards_rows"] if r["lift"] == BW_LIFT)
        assert all(c["weight"] == "-15" for c in row["cells"])
