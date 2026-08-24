"""Tests for the challenge detail personal performance section (TASK-29,
TASK-248).

Every challenge is CUSTOM (TASK-248): thresholds are a flat per-lift,
per-rep target table, never a bodyweight-scaled multiplier. Fixtures below
materialise that flat table via tier_thresholds — the same Epley expansion
the old built-in path used — so most of this file's numbers and assertions
carry over unchanged; only the mechanism producing them changed. For
bodyweight-added lifts the table is additionally shifted so the stored/
performed number is the added weight directly (TASK-248 — no bodyweight
arithmetic survives anywhere in scoring or display).
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from accounts.units import from_display_weight, to_display_weight
from challenges.models import Challenge, ChallengeParticipant
from challenges.services import (
    _flag_close_to_goal,
    _flag_endgame_suggestion,
    _kg_to_display,
    _next_point_gap,
    _weight_display,
)
from challenges.tests.factories import (
    ChallengeLiftFactory,
    ChallengeParticipantFactory,
    CustomGoalFactory,
    CustomGoalTargetFactory,
    make_custom_challenge,
)
from liftosaur.tests.factories import LiftHistoryFactory
from scoring.domain.calculator import threshold_for_reps, tier_thresholds
from scoring.tests.factories import PointEarnEventFactory

# The documented defaults, pinned in tests via the ``settings`` fixture so they
# stay deterministic regardless of any ambient CHALLENGES_CLOSE_TO_GOAL_*
# override in the runtime .env.
CLOSE_TO_GOAL_GAP_FRACTION = Decimal("0.05")
CLOSE_TO_GOAL_REPS_GAP = 2

# Endgame-suggestion defaults, pinned the same way (TASK-212).
ENDGAME_WINDOW_DAYS = 14
ENDGAME_GAP_FRACTION = Decimal("0.05")
ENDGAME_REPS_GAP = 2


def _flat_targets(tier, multiplier, bodyweight):
    """A flat {rep: weight} table equivalent to the old multiplier x bodyweight
    threshold, via the same Epley expansion (tier_thresholds)."""
    thresholds = tier_thresholds(tier, Decimal(multiplier), Decimal(bodyweight))
    return {rm.reps: rm.weight for rm in thresholds.rep_maxes}


def _added_weight_targets(tier, multiplier, bodyweight):
    """Same as _flat_targets, shifted to added weight for bodyweight-added
    lifts — the conversion goal-setup performs once at materialisation
    (challenges.goal_builders.suggest_from_standards)."""
    bodyweight = Decimal(bodyweight)
    return {
        reps: weight - bodyweight
        for reps, weight in _flat_targets(tier, multiplier, bodyweight).items()
    }


def _attach_targets(goal, lift, targets):
    for rep, weight in targets.items():
        CustomGoalTargetFactory(
            goal=goal, lift=lift, rep_count=rep, target_weight=weight
        )


def _expected_threshold(
    rep_count, tier="Intermediate", multiplier="1.5000", bodyweight="100.00"
):
    """The displayed weight for a flat target cell: unit-converted (kg here) and
    rounded to 0.1 precision, never plate-grid snapped. Every threshold is a
    hand-authored/materialised CustomGoalTarget now (TASK-248) -- the same kind
    of number a real recorded weight is, so display never applies a plate-grid
    snap (challenges/services.py always calls with snap=False)."""
    target = _flat_targets(tier, multiplier, bodyweight)[rep_count]
    return target.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _expected_gap(
    rep_count,
    actual_weight,
    tier="Intermediate",
    multiplier="1.5000",
    bodyweight="100.00",
):
    """The exact, never-plate-snapped weight_gap for a flat target at
    ``rep_count`` against ``actual_weight`` -- mirrors _expected_threshold."""
    target = _flat_targets(tier, multiplier, bodyweight)[rep_count]
    gap = target - Decimal(actual_weight)
    if gap < Decimal("0"):
        gap = Decimal("0")
    return gap.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


@pytest.fixture
def user(db):
    return UserFactory(unit_preference="kg")


@pytest.fixture
def challenge(db, user):
    return make_custom_challenge(
        lifts=["Squat"], creator=user, status=Challenge.Status.ACTIVE
    )


@pytest.fixture
def participant(challenge, user):
    participant = ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        joined_at=timezone.now() - timedelta(days=30),
    )
    goal = CustomGoalFactory(participant=participant, name="Intermediate")
    _attach_targets(goal, "Squat", _flat_targets("Intermediate", "1.5000", "100.00"))
    participant.custom_goal = goal
    participant.save(update_fields=["custom_goal"])
    return participant


@pytest.fixture
def authed_client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def mock_sync():
    with patch("liftosaur.services.LiftosaurClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.get_weight_measurements.return_value = ([], False, None)
        mock_client.get_history.return_value = ([], False, None)
        yield mock_client


@pytest.fixture
def squat_multiplier(participant):
    """No-op marker (TASK-248): `participant` already materialises Squat's
    flat targets from the equivalent multiplier x bodyweight table. Kept so
    existing test signatures below don't all need editing."""
    return None


def _get(authed_client, challenge):
    url = reverse("challenges:detail", args=[challenge.pk])
    return authed_client.get(url)


class TestSummaryCards:
    def test_card_with_existing_points(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=5,
            weight=Decimal("120.00"),
            points_earned=6,
            is_current_best=True,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        cards = resp.context["personal_data"]["summary_cards"]
        squat = next(c for c in cards if c["lift"] == "Squat")
        assert squat["state"] == "scored"
        assert squat["points_earned"] == 6
        assert squat["weight"] == Decimal("120.0")
        assert squat["reps"] == 5
        assert squat["date"] == timezone.now().date()
        assert squat["tier_satisfied"] == "Intermediate"
        assert "6 pts" in resp.content.decode()
        assert "met_by_tolerance" not in squat
        assert "Counted within scoring tolerance" not in resp.content.decode()

    def test_card_with_no_points_shows_gap(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=8,
            weight=Decimal("80.00"),
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        cards = resp.context["personal_data"]["summary_cards"]
        squat = next(c for c in cards if c["lift"] == "Squat")
        assert squat["state"] == "no_points"
        assert squat["best_weight"] == Decimal("80.0")
        assert squat["best_reps"] == 8
        assert squat["best_date"] == timezone.now().date()
        # one_rm = 1.5 * 100 = 150. The weight gap is measured at the reps the set
        # was actually performed at (8), not the reps-agnostic 10RM floor.
        expected_gap = _expected_gap(8, "80.00")
        assert squat["weight_gap"] == expected_gap
        # 80kg is below the 10RM floor (112.5), so no rep count would earn the
        # point: only the weight path is shown.
        assert squat["reps_gap"] is None
        body = resp.content.decode()
        assert '× <span class="font-mono">8</span> reps' in body

    def test_best_weight_uses_highest_across_all_events(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        for w in (Decimal("60.00"), Decimal("90.00"), Decimal("75.00")):
            PointEarnEventFactory(
                user=user,
                challenge=challenge,
                lift="Squat",
                weight=w,
                points_earned=0,
                is_current_best=False,
                performed_at=timezone.now().date(),
            )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["best_weight"] == Decimal("90.0")

    def test_card_with_no_data(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_data"
        assert "No data yet" in resp.content.decode()

    def test_gap_clamped_at_zero_when_over_threshold(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=10,
            weight=Decimal("200.00"),
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        # Weight already exceeds the threshold at these reps -> gap clamps to zero
        # rather than going negative.
        assert squat["weight_gap"] == Decimal("0.0")

    def test_lb_display_unit_conversion(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        user.unit_preference = "lb"
        user.save(update_fields=["unit_preference"])
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=5,
            weight=Decimal("100.00"),
            points_earned=6,
            is_current_best=True,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        data = resp.context["personal_data"]
        assert data["display_unit"] == "lb"
        squat = next(c for c in data["summary_cards"] if c["lift"] == "Squat")
        # 100 kg -> lb
        from accounts.units import to_display_weight

        assert squat["weight"] == to_display_weight(Decimal("100.00"), "lb")[0]


class TestLiftHistoryFallback:
    def test_no_events_but_history_shows_no_points_with_gap(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # Pooled sub-threshold LiftHistory within the window is now scored on
        # view load (TASK-92), producing a zero-point audit event -> the card
        # shows no_points with a gap driven by that event.
        LiftHistoryFactory(
            user=user,
            lift="Squat",
            weight_kg=Decimal("80.00"),
            reps=8,
            performed_at=(timezone.now() - timedelta(days=10)).date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_points"
        assert squat["best_weight"] == Decimal("80.0")
        expected_gap = _expected_gap(8, "80.00")
        assert squat["weight_gap"] == expected_gap
        assert squat["reps_gap"] is None

    def test_pooled_history_best_weight_scored_on_view(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # All three pooled sets are sub-threshold (150kg 1RM threshold), so they
        # score as zero-point audit events; the card surfaces the heaviest.
        for i, w in enumerate((Decimal("60.00"), Decimal("95.00"), Decimal("70.00"))):
            LiftHistoryFactory(
                user=user,
                lift="Squat",
                weight_kg=w,
                reps=5,
                performed_at=(timezone.now() - timedelta(days=i + 1)).date(),
            )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_points"
        assert squat["best_weight"] == Decimal("95.0")

    def test_unscored_history_falls_back_when_sync_skipped(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # When the on-view sync is skipped (recent successful sync within the
        # cooldown) the pool is not re-scored, so a lift with pooled history but
        # no PointEarnEvents still surfaces a gap via the raw-history fallback.
        from liftosaur.tests.factories import LiftosaurSyncLogFactory

        LiftHistoryFactory(
            user=user,
            lift="Squat",
            weight_kg=Decimal("80.00"),
            reps=8,
            performed_at=(timezone.now() - timedelta(days=10)).date(),
        )
        LiftosaurSyncLogFactory(
            user=user,
            success=True,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_points"
        assert squat["best_weight"] == Decimal("80.0")
        expected_gap = _expected_gap(8, "80.00")
        assert squat["weight_gap"] == expected_gap
        assert squat["reps_gap"] is None

    def test_history_outside_window_shows_no_data_before_window(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # LiftHistory that predates the participant's window is real data the
        # scorer will never see, so it must surface the distinct
        # no_data_before_window state (TASK-107) rather than the bare no_data
        # state, which is indistinguishable from "never logged anything".
        LiftHistoryFactory(
            user=user,
            lift="Squat",
            weight_kg=Decimal("80.00"),
            reps=8,
            performed_at=(timezone.now() - timedelta(days=200)).date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_data_before_window"
        assert squat["window_start_date"] == participant.joined_at.date()

    def test_no_history_still_shows_no_data(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_data"

    def test_qualifying_pooled_history_scored_on_view(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # A qualifying pooled set (200kg 1RM over the 150kg threshold) is scored
        # on view load into a current-best event, so the card reads "scored"
        # rather than falling back to raw history or an earlier sub-threshold row.
        LiftHistoryFactory(
            user=user,
            lift="Squat",
            weight_kg=Decimal("200.00"),
            reps=1,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "scored"
        assert squat["points_earned"] == 10
        assert squat["weight"] == Decimal("200.0")


@pytest.fixture
def pullup_multiplier(participant, challenge):
    """Attaches Pull-up (a bodyweight-added lift) to the participant's goal
    with added-weight targets equivalent to a 1.5x80kg multiplier table."""
    ChallengeLiftFactory(challenge=challenge, name="Pull-up")
    _attach_targets(
        participant.custom_goal,
        "Pull-up",
        _added_weight_targets("Intermediate", "1.5000", "80.00"),
    )
    return None


class TestTwoDimensionalGap:
    """AC#1–#5, #8: gap reported as two independent paths to the first point."""

    def test_pullup_clears_floor_but_low_reps_shows_both_paths(
        self,
        authed_client,
        participant,
        challenge,
        user,
        pullup_multiplier,
        mock_sync,
    ):
        # AC#3: a Pull-up that clears the 10RM weight but is only 5 reps must show
        # a real shortfall — a nonzero weight gap at 5 reps AND the reps needed —
        # never "0 short".
        # Added weight +20 (total load equivalent 100 at 80kg bodyweight),
        # one_rm = 1.5*80 = 120 (total-load terms; the added-weight terms used
        # below differ by the same constant on both sides, so the gap math is
        # identical either way — see TASK-248 plan §1a).
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Pull-up",
            reps=5,
            weight=Decimal("20.00"),
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        card = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Pull-up"
        )
        assert card["state"] == "no_points"
        assert card["best_weight"] == "+20"
        expected_weight_gap = _expected_gap(5, "100.00", bodyweight="80.00")
        assert card["weight_gap"] == expected_weight_gap
        assert card["weight_gap"] > Decimal("0")
        # 100kg clears the 10RM floor (90), and threshold_for_reps(120, 6) == 100,
        # so one more rep (5 -> 6) also earns the point.
        assert card["reps_gap"] == 1

    def test_heavy_low_rep_set_over_floor_reports_reps_path(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # AC#8: a heavy, low-rep set that clears the 10RM weight but not its own
        # rep threshold reports a nonzero weight gap plus the reps path.
        # one_rm = 150, 10RM floor = 112.5. 115kg x 3 clears the floor but not the
        # 3-rep threshold (136.36).
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=3,
            weight=Decimal("115.00"),
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_points"
        expected_weight_gap = _expected_gap(3, "115.00")
        assert squat["weight_gap"] == expected_weight_gap
        assert squat["weight_gap"] > Decimal("0")
        # threshold_for_reps(150, 10) == 112.5 <= 115, so a lighter rep-max
        # threshold is already met at the current weight. The comparison is now
        # exact (no fuzz band): 115kg does not reach threshold_for_reps(150, 9)
        # == 115.38, so the first rep count it clears is 10, needing 7 more reps
        # (3 -> 10).
        assert squat["reps_gap"] == 7

    def test_set_nearest_to_a_point_chosen_not_heaviest(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # AC#2: the gap is computed from the set nearest to a point, not blindly
        # the heaviest. A heavy low-rep set (130x1) is further from a point than a
        # lighter high-rep set (120x8), so the latter drives the gap.
        for reps, weight in ((1, Decimal("130.00")), (8, Decimal("120.00"))):
            PointEarnEventFactory(
                user=user,
                challenge=challenge,
                lift="Squat",
                reps=reps,
                weight=weight,
                points_earned=0,
                is_current_best=False,
                performed_at=timezone.now().date(),
            )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        # 120x8: threshold(8)=118.42, gap=0 (clamped, actually scores) vs 130x1
        # threshold(1)=150, gap 20. The 8-rep set is nearest, so best_weight=120.
        assert squat["best_weight"] == Decimal("120.0")
        expected_weight_gap = _expected_gap(8, "120.00")
        assert squat["weight_gap"] == expected_weight_gap

    def test_scoring_set_never_renders_phantom_gap(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # AC#4: a pooled set that best_score_for_set would score is scored on view
        # into a current best and renders "scored", never a no_points phantom gap.
        LiftHistoryFactory(
            user=user,
            lift="Squat",
            weight_kg=Decimal("140.00"),
            reps=5,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "scored"
        assert "weight_gap" not in squat


class TestFallbackAlignment:
    """AC#6, #7: the LiftHistory fallback shares the gap computation and aligns
    its window + bodyweight filters with score_pooled_history."""

    def test_fallback_reports_two_dimensional_gap(
        self, participant, challenge, user, squat_multiplier
    ):
        from challenges.services import build_personal_data

        history_date = (timezone.now() - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Squat",
            weight_kg=Decimal("80.00"),
            reps=8,
            performed_at=history_date,
        )
        # Direct call bypasses the view's score_pooled_history, exercising the raw
        # LiftHistory fallback branch.
        data = build_personal_data(user, challenge, participant)
        squat = next(c for c in data["summary_cards"] if c["lift"] == "Squat")
        assert squat["state"] == "no_points"
        assert squat["best_weight"] == Decimal("80.0")
        assert squat["best_reps"] == 8
        assert squat["best_date"] == history_date
        expected_gap = _expected_gap(8, "80.00")
        assert squat["weight_gap"] == expected_gap
        assert squat["reps_gap"] is None

    def test_fallback_excludes_rows_before_challenge_window(
        self, participant, challenge, user, squat_multiplier
    ):
        from challenges.services import build_personal_data

        # Performed before the participant's window_start (joined 30 days ago):
        # scoring would never see it, so the fallback must not either. It does
        # however surface the distinct no_data_before_window state (TASK-107)
        # since real history exists, just outside the window.
        LiftHistoryFactory(
            user=user,
            lift="Squat",
            weight_kg=Decimal("80.00"),
            reps=8,
            performed_at=(timezone.now() - timedelta(days=60)).date(),
        )
        data = build_personal_data(user, challenge, participant)
        squat = next(c for c in data["summary_cards"] if c["lift"] == "Squat")
        assert squat["state"] == "no_data_before_window"
        assert squat["window_start_date"] == participant.joined_at.date()


class TestStandardsTable:
    def test_threshold_values_match_calculator(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        resp = _get(authed_client, challenge)
        data = resp.context["personal_data"]
        assert [col["reps"] for col in data["rep_columns"]] == list(range(10, 0, -1))
        # 1RM is worth 10 points down to 10RM worth 1 (points_for_rep_count),
        # so the point labels ascend as the rep columns descend.
        assert [col["points"] for col in data["rep_columns"]] == list(range(1, 11))
        row = next(r for r in data["standards_rows"] if r["lift"] == "Squat")
        for cell in row["cells"]:
            assert cell["weight"] == _expected_threshold(cell["reps"])

    def test_standards_columns_descending_10rm_first(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # Standards table must render 10RM (easiest) leftmost through 1RM
        # (hardest) rightmost, matching the goal-setup page (TASK-99).
        resp = _get(authed_client, challenge)
        data = resp.context["personal_data"]
        assert [col["reps"] for col in data["rep_columns"]] == list(range(10, 0, -1))
        assert data["rep_columns"][0]["reps"] == 10
        assert data["rep_columns"][-1]["reps"] == 1
        row = next(r for r in data["standards_rows"] if r["lift"] == "Squat")
        assert [c["reps"] for c in row["cells"]] == list(range(10, 0, -1))

    def test_current_best_cell_highlighted(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=5,
            weight=Decimal("130.00"),
            points_earned=6,  # rep-max satisfied = 11 - 6 = 5
            is_current_best=True,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        row = next(
            r
            for r in resp.context["personal_data"]["standards_rows"]
            if r["lift"] == "Squat"
        )
        highlighted = [c["reps"] for c in row["cells"] if c["is_current_best"]]
        assert highlighted == [5]


class TestParticipationWindow:
    def test_current_best_before_window_still_scored(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # A current-best event whose performed_at precedes window_start only
        # arises after a bail+rejoin (rejoin resets joined_at to now); the event
        # still counts on the leaderboard and points-over-time chart, so Your
        # Performance must render it too rather than hide it behind the reset
        # window (TASK-164).
        before = (participant.joined_at - timedelta(days=5)).date()
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=5,
            weight=Decimal("140.00"),
            points_earned=6,
            is_current_best=True,
            performed_at=before,
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "scored"
        assert squat["points_earned"] == 6

    def test_bail_then_rejoin_keeps_scored_history_consistent(
        self,
        participant,
        challenge,
        user,
        squat_multiplier,
    ):
        # End-to-end regression for the reported bug (TASK-164): a participant
        # scored before bailing, then rejoined (joined_at reset to now). Their
        # pre-bail current-best must stay visible in Your Performance and match
        # the points-over-time chart, both of which the leaderboard credits.
        from challenges.services import build_personal_data
        from scoring.services import build_points_over_time

        pre_bail = (participant.joined_at + timedelta(days=1)).date()
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=5,
            weight=Decimal("140.00"),
            points_earned=6,
            is_current_best=True,
            performed_at=pre_bail,
        )

        # bail
        participant.is_bailed = True
        participant.bailed_at = timezone.now()
        participant.save(update_fields=["is_bailed", "bailed_at"])

        # rejoin (mirrors invite_link_view: window restarts at now)
        participant.is_bailed = False
        participant.bailed_at = None
        participant.invite_status = ChallengeParticipant.InviteStatus.ACCEPTED
        participant.joined_at = timezone.now()
        participant.save(
            update_fields=["is_bailed", "bailed_at", "invite_status", "joined_at"]
        )

        data = build_personal_data(user, challenge, participant)
        squat = next(c for c in data["summary_cards"] if c["lift"] == "Squat")
        assert squat["state"] == "scored"
        assert squat["points_earned"] == 6

        chart = build_points_over_time(challenge)
        by_label = {ds["label"]: ds["data"] for ds in chart["datasets"]}
        # leading zero baseline, then the single credited event
        assert by_label[user.display_name or user.username] == [0, 6]

    def test_event_on_join_date_included(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=5,
            weight=Decimal("140.00"),
            points_earned=6,
            is_current_best=True,
            performed_at=participant.joined_at.date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "scored"

    def test_from_start_includes_pre_join_events(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        challenge.history_window = Challenge.HistoryWindow.FROM_START
        challenge.start_date = (participant.joined_at - timedelta(days=20)).date()
        challenge.save(update_fields=["history_window", "start_date"])
        # Performed before join but after challenge start -> included now.
        before_join = (participant.joined_at - timedelta(days=5)).date()
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=5,
            weight=Decimal("140.00"),
            points_earned=6,
            is_current_best=True,
            performed_at=before_join,
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "scored"

    def test_from_start_current_best_before_start_still_scored(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # A current-best event predating the challenge start still counts on
        # the leaderboard and chart (neither window-filters), so Your Performance
        # renders it too rather than diverging from them (TASK-164). Only the
        # unscored gap/fallback states remain window-scoped.
        challenge.history_window = Challenge.HistoryWindow.FROM_START
        challenge.start_date = (participant.joined_at - timedelta(days=20)).date()
        challenge.save(update_fields=["history_window", "start_date"])
        before_start = (participant.joined_at - timedelta(days=30)).date()
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=5,
            weight=Decimal("140.00"),
            points_earned=6,
            is_current_best=True,
            performed_at=before_start,
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "scored"
        assert squat["points_earned"] == 6


class TestNoDataBeforeWindow:
    """TASK-107: distinguish 'never logged this lift' from 'logged it, but
    only before this participant's challenge window opened'."""

    def test_from_join_history_only_before_join_shows_distinct_state(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # from_join (the fixture default): the participant joined 30 days ago,
        # and their only Squat history is from 60 days ago -> before the join
        # date. That's real history the scorer will never see, so the card
        # must say so instead of rendering the bare "No data yet" state.
        LiftHistoryFactory(
            user=user,
            lift="Squat",
            weight_kg=Decimal("80.00"),
            reps=8,
            performed_at=(timezone.now() - timedelta(days=60)).date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_data_before_window"
        assert squat["window_start_date"] == participant.joined_at.date()
        content = resp.content.decode()
        assert participant.joined_at.date().strftime("%b %-d, %Y") in content

    def test_from_start_history_only_before_challenge_start_shows_distinct_state(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # from_start: the window opens at the challenge's start date rather
        # than the join date. History that predates start_date must still be
        # called out with the window_start_date the card actually used.
        challenge.history_window = Challenge.HistoryWindow.FROM_START
        challenge.start_date = (timezone.now() - timedelta(days=20)).date()
        challenge.save(update_fields=["history_window", "start_date"])
        LiftHistoryFactory(
            user=user,
            lift="Squat",
            weight_kg=Decimal("80.00"),
            reps=8,
            performed_at=(timezone.now() - timedelta(days=40)).date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_data_before_window"
        assert squat["window_start_date"] == challenge.start_date


class TestPersonalDataGuards:
    def test_none_when_no_custom_goal(self, db, challenge, user):
        # A participant who hasn't completed goal setup has no CustomGoal yet;
        # build_personal_data returns None until they do.
        from challenges.services import build_personal_data

        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=timezone.now(),
        )
        assert build_personal_data(user, challenge, participant) is None


class TestTabToggle:
    def test_both_tab_divs_and_js_present(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        content = _get(authed_client, challenge).content.decode()
        assert 'id="tab-summary"' in content
        assert 'id="tab-standards"' in content
        assert "showPersonalTab" in content


@pytest.fixture
def chinup_multiplier(challenge, participant):
    """Attaches flat Chin-up (bodyweight-added) targets to the shared goal,
    equivalent to the old 1.5x-bodyweight multiplier ladder (TASK-248)."""
    ChallengeLiftFactory(challenge=challenge, name="Chin-up")
    _attach_targets(
        participant.custom_goal,
        "Chin-up",
        _added_weight_targets("Intermediate", "1.5000", "100.00"),
    )
    return participant.custom_goal


class TestBodyweightAddedDisplay:
    """The BW/+N/-N display convention survives unchanged (TASK-248): the
    stored/recorded number for a bodyweight-added lift IS the added weight
    directly, so these assertions use added-weight literals rather than the
    old total-load-minus-bodyweight ones."""

    def test_scored_card_shows_added_weight(
        self,
        authed_client,
        participant,
        challenge,
        user,
        chinup_multiplier,
        mock_sync,
    ):
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Chin-up",
            reps=3,
            weight=Decimal("5.00"),
            points_earned=8,
            is_current_best=True,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        card = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Chin-up"
        )
        assert card["weight"] == "+5"
        assert card["is_bodyweight_added"] is True

    def test_bodyweight_only_shows_bw(
        self,
        authed_client,
        participant,
        challenge,
        user,
        chinup_multiplier,
        mock_sync,
    ):
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Chin-up",
            reps=3,
            weight=Decimal("0.00"),
            points_earned=8,
            is_current_best=True,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        card = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Chin-up"
        )
        assert card["weight"] == "BW"

    def test_assisted_shows_negative(
        self,
        authed_client,
        participant,
        challenge,
        user,
        chinup_multiplier,
        mock_sync,
    ):
        # Band-assisted work stays legal and visible as a negative added
        # weight — only leverage-machine equipment is excluded from scoring.
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Chin-up",
            reps=8,
            weight=Decimal("-10.00"),
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        card = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Chin-up"
        )
        assert card["state"] == "no_points"
        assert card["best_weight"] == "-10"

    def test_standards_cells_show_added_weight(
        self,
        authed_client,
        participant,
        challenge,
        user,
        chinup_multiplier,
        mock_sync,
    ):
        resp = _get(authed_client, challenge)
        row = next(
            r
            for r in resp.context["personal_data"]["standards_rows"]
            if r["lift"] == "Chin-up"
        )
        assert row["is_bodyweight_added"] is True
        # 1RM threshold = 1.5 * 100 - 100 bodyweight = 50 added.
        one_rm_cell = next(c for c in row["cells"] if c["reps"] == 1)
        assert one_rm_cell["weight"] == "+50"
        # Template renders the added marker, not "150 kg".
        content = resp.content.decode()
        assert "(BW)" in content


class TestPlateGridSnapFidelity:
    """Actual recorded weights must not pick up plate-grid drift (TASK-120).

    ``_kg_to_display`` can snap to the challenge's ``2 x smallest_plate``
    grid, which used to be correct for a *computed* plate-loadable threshold
    but was always wrong for real logged data: a challenge configured in lb
    stores ``smallest_plate`` as a kg value with only 2 decimals (e.g. 2.5 lb
    -> 1.13 kg), so snapping a clean logged 95 lb to that kg grid and back
    drifts it to ~94.6.

    As of TASK-248 every challenge is CUSTOM: a threshold cell reads a
    hand-authored/materialised CustomGoalTarget, the same kind of number a
    real recorded weight is, so ``challenges/services.py`` now calls every
    display helper with ``snap=False`` unconditionally -- ``snap=True``
    (kept on ``_kg_to_display``/``_weight_display`` as a documented, no-
    longer-exercised affordance) is never actually passed by production code.
    """

    # 2.5 lb expressed in kg the way the create form would store it.
    LB_PLATE_KG = Decimal("1.13")

    def test_kg_to_display_actual_weight_no_grid_drift(self, challenge):
        challenge.smallest_plate = self.LB_PLATE_KG
        # 95 lb as stored kg (what a logged 95 lb set persists as).
        ninety_five_lb_kg = from_display_weight(Decimal("95"), "lb")
        # snap=False: pure unit conversion + 0.1 rounding, no drift.
        assert _kg_to_display(
            ninety_five_lb_kg, "lb", challenge, snap=False
        ) == Decimal("95.0")
        # snap=True (the old, buggy behavior for real data) drifts it downward.
        assert _kg_to_display(ninety_five_lb_kg, "lb", challenge, snap=True) < Decimal(
            "95.0"
        )

    def test_weight_display_actual_weight_no_grid_drift(self, challenge):
        challenge.smallest_plate = self.LB_PLATE_KG
        deadlift_kg = from_display_weight(Decimal("235"), "lb")
        displayed = _weight_display(
            deadlift_kg, "Deadlift", "lb", challenge, snap=False
        )
        assert displayed == Decimal("235.0")

    def test_threshold_cell_is_never_plate_snapped(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # A challenge configured in lb with a 2.5 lb smallest plate.
        challenge.smallest_plate = self.LB_PLATE_KG
        challenge.plate_unit = "lb"
        challenge.save(update_fields=["smallest_plate", "plate_unit"])
        user.unit_preference = "lb"
        user.save(update_fields=["unit_preference"])
        # Log an actual 95 lb squat as the current best.
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=5,
            weight=from_display_weight(Decimal("95"), "lb"),
            points_earned=6,
            is_current_best=True,
            performed_at=timezone.now().date(),
        )
        data = _get(authed_client, challenge).context["personal_data"]
        squat_card = next(c for c in data["summary_cards"] if c["lift"] == "Squat")
        # Real logged weight renders clean, no plate-grid drift.
        assert squat_card["weight"] == Decimal("95.0")
        # The threshold cell is the participant's own flat 1RM target, unit-
        # converted with no plate-grid snap -- the same convention as real data.
        row = next(r for r in data["standards_rows"] if r["lift"] == "Squat")
        one_rm_cell = next(c for c in row["cells"] if c["reps"] == 1)
        one_rm_kg = _flat_targets("Intermediate", "1.5000", "100.00")[1]
        assert one_rm_cell["weight"] == to_display_weight(one_rm_kg, "lb")[0]


def _close_card(lift, *, state="no_points", gap_fraction=None, reps_gap=None):
    """Minimal summary-card dict carrying only the keys _flag_close_to_goal reads."""
    return {
        "lift": lift,
        "state": state,
        "gap_fraction": gap_fraction,
        "reps_gap": reps_gap,
    }


class TestFlagCloseToGoal:
    """Unit coverage for the close-to-goal qualification, boundary, and cap logic.

    Works on hand-built card dicts so the gate (state filter, inclusive 5%
    boundary, reps_gap<=2 override, None-gap exclusion) and the count cap are
    exercised deterministically, free of plate-snap / Epley quantization. The
    raw-kg pre-snap computation of ``gap_fraction`` itself is covered end-to-end
    by :class:`TestCloseToGoalIntegration` and the custom-source suite.
    """

    @pytest.fixture(autouse=True)
    def _pin_thresholds(self, settings):
        # Pin to the documented defaults so the literal gap_fraction / reps_gap
        # values below are meaningful regardless of any ambient env override.
        settings.CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION = float(
            CLOSE_TO_GOAL_GAP_FRACTION
        )
        settings.CHALLENGES_CLOSE_TO_GOAL_REPS_GAP = CLOSE_TO_GOAL_REPS_GAP

    def test_nothing_qualifies_flags_nothing(self):
        cards = [
            _close_card("Squat", gap_fraction=Decimal("0.10")),
            _close_card("Bench Press", gap_fraction=Decimal("0.20"), reps_gap=3),
        ]
        _flag_close_to_goal(cards)
        assert all("close_to_goal" not in c for c in cards)

    def test_single_qualifier_is_flagged(self):
        cards = [
            _close_card("Squat", gap_fraction=Decimal("0.03")),
            _close_card("Bench Press", gap_fraction=Decimal("0.30")),
        ]
        _flag_close_to_goal(cards)
        assert cards[0]["close_to_goal"] is True
        assert "close_to_goal" not in cards[1]

    def test_cap_keeps_only_the_three_closest(self):
        cards = [
            _close_card("Bench Press", gap_fraction=Decimal("0.05")),
            _close_card("Deadlift", gap_fraction=Decimal("0.01")),
            _close_card("Overhead Press", gap_fraction=Decimal("0.04")),
            _close_card("Row", gap_fraction=Decimal("0.02")),
            _close_card("Squat", gap_fraction=Decimal("0.03")),
        ]
        _flag_close_to_goal(cards)
        flagged = {c["lift"] for c in cards if c.get("close_to_goal")}
        # The three smallest fractions (0.01, 0.02, 0.03), never all five.
        assert flagged == {"Deadlift", "Row", "Squat"}

    def test_boundary_exactly_at_threshold_qualifies(self):
        card = _close_card("Squat", gap_fraction=CLOSE_TO_GOAL_GAP_FRACTION)
        _flag_close_to_goal([card])
        assert card["close_to_goal"] is True

    def test_just_over_boundary_does_not_qualify(self):
        card = _close_card(
            "Squat", gap_fraction=CLOSE_TO_GOAL_GAP_FRACTION + Decimal("0.001")
        )
        _flag_close_to_goal([card])
        assert "close_to_goal" not in card

    def test_one_rep_away_qualifies_despite_large_weight_fraction(self):
        card = _close_card("Squat", gap_fraction=Decimal("0.40"), reps_gap=1)
        _flag_close_to_goal([card])
        assert card["close_to_goal"] is True

    def test_two_reps_away_qualifies_at_the_reps_boundary(self):
        card = _close_card("Squat", gap_fraction=Decimal("0.40"), reps_gap=2)
        _flag_close_to_goal([card])
        assert card["close_to_goal"] is True

    def test_three_reps_away_does_not_qualify(self):
        card = _close_card("Squat", gap_fraction=Decimal("0.40"), reps_gap=3)
        _flag_close_to_goal([card])
        assert "close_to_goal" not in card

    def test_reps_gap_threshold_is_read_from_settings(self, settings):
        # With the reps threshold tightened to 1, a two-rep-away lift that would
        # qualify under the default no longer does — proving the setting is read
        # rather than a hardcoded constant. The large weight fraction keeps the
        # weight path from qualifying it independently.
        settings.CHALLENGES_CLOSE_TO_GOAL_REPS_GAP = 1
        card = _close_card("Squat", gap_fraction=Decimal("0.40"), reps_gap=2)
        _flag_close_to_goal([card])
        assert "close_to_goal" not in card

    def test_gap_fraction_threshold_is_read_from_settings(self, settings):
        # A 0.08 gap exceeds the 0.05 default but falls inside a widened 0.10
        # band, so it only qualifies when the setting is actually consulted.
        settings.CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION = 0.10
        card = _close_card("Squat", gap_fraction=Decimal("0.08"))
        _flag_close_to_goal([card])
        assert card["close_to_goal"] is True

    def test_scored_card_is_never_flagged(self):
        card = _close_card("Squat", state="scored", gap_fraction=Decimal("0.01"))
        _flag_close_to_goal([card])
        assert "close_to_goal" not in card

    def test_no_data_before_window_card_is_never_flagged(self):
        card = _close_card(
            "Squat",
            state="no_data_before_window",
            gap_fraction=Decimal("0.01"),
            reps_gap=1,
        )
        _flag_close_to_goal([card])
        assert "close_to_goal" not in card

    def test_none_gap_fraction_is_never_flagged(self):
        card = _close_card("Squat", gap_fraction=None, reps_gap=1)
        _flag_close_to_goal([card])
        assert "close_to_goal" not in card


class TestCloseToGoalIntegration:
    """End-to-end: a near-threshold no_points card is flagged and renders a badge,
    with gap_fraction computed from the raw pre-snap kg gap."""

    @pytest.fixture(autouse=True)
    def _pin_thresholds(self, settings):
        settings.CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION = float(
            CLOSE_TO_GOAL_GAP_FRACTION
        )
        settings.CHALLENGES_CLOSE_TO_GOAL_REPS_GAP = CLOSE_TO_GOAL_REPS_GAP

    def test_close_no_points_card_flagged_and_badge_rendered(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # one_rm = 1.5 * 100 = 150; threshold_at(10) = 112.5. A 110 kg set at 10
        # reps sits 2.5 kg short -> gap_fraction = 2.5/112.5 ~= 0.022, well within
        # the 5% band, so the card is flagged.
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=10,
            weight=Decimal("110.00"),
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_points"
        one_rm = Decimal("1.5000") * Decimal("100.00")
        t10 = threshold_for_reps(one_rm, 10)
        # Computed from the raw pre-snap kg gap, not the plate-snapped display gap.
        assert squat["gap_fraction"] == (t10 - Decimal("110.00")) / t10
        assert squat["gap_fraction"] <= CLOSE_TO_GOAL_GAP_FRACTION
        assert squat["close_to_goal"] is True
        assert "Close to goal" in resp.content.decode()


def _endgame_scored_card(
    lift, *, points_earned=5, next_point_gap_fraction=None, next_point_weight_gap=None
):
    """Minimal scored card carrying only the keys the endgame flag reads."""
    return {
        "lift": lift,
        "state": "scored",
        "points_earned": points_earned,
        "next_point_gap_fraction": next_point_gap_fraction,
        "next_point_weight_gap": next_point_weight_gap,
    }


class TestNextPointGap:
    """Unit coverage for the scored-lift next-point weight gap (TASK-212)."""

    def test_targets_the_next_point_up_rep_count(self):
        # thresholds keyed by rep count so we can prove which rep target is used.
        thresholds = {r: Decimal("10") * r for r in range(1, 11)}
        best = SimpleNamespace(points_earned=5, weight=Decimal("48"))
        # next point p+1 = 6 -> reps = 11 - 6 = 5 -> target 50; gap 2.
        gap, fraction = _next_point_gap(
            lambda reps: thresholds[reps], best, "kg", None, snap=False
        )
        expected_gap, _ = to_display_weight(Decimal("2"), "kg")
        assert gap == expected_gap
        assert fraction == Decimal("2") / Decimal("50")

    def test_clamped_at_zero_when_already_over_target(self):
        best = SimpleNamespace(points_earned=5, weight=Decimal("60"))
        gap, fraction = _next_point_gap(
            lambda reps: Decimal("50"), best, "kg", None, snap=False
        )
        expected_zero, _ = to_display_weight(Decimal("0"), "kg")
        assert gap == expected_zero
        assert fraction == Decimal("0")

    def test_at_max_tier_returns_none(self):
        best = SimpleNamespace(points_earned=10, weight=Decimal("100"))
        assert _next_point_gap(
            lambda reps: Decimal("50"), best, "kg", None, snap=False
        ) == (None, None)

    def test_no_threshold_returns_none(self):
        best = SimpleNamespace(points_earned=5, weight=Decimal("48"))
        assert _next_point_gap(None, best, "kg", None, snap=False) == (None, None)


class TestFlagEndgameSuggestion:
    """Unit coverage for endgame-suggestion qualification, window gate, and cap.

    Builds unsaved ``Challenge`` instances rather than going through the
    factory: ``_flag_endgame_suggestion`` only reads ``status`` and
    ``end_date`` off the challenge, so persisting one (plus the creator the
    factory drags along) buys nothing but round-trips. Matches
    :class:`TestFlagCloseToGoal`, which works on hand-built cards for the
    same reason.
    """

    @pytest.fixture(autouse=True)
    def _pin_thresholds(self, settings):
        settings.CHALLENGES_ENDGAME_WINDOW_DAYS = ENDGAME_WINDOW_DAYS
        settings.CHALLENGES_ENDGAME_GAP_FRACTION = float(ENDGAME_GAP_FRACTION)
        settings.CHALLENGES_ENDGAME_REPS_GAP = ENDGAME_REPS_GAP

    def _in_window(self):
        return Challenge(
            status=Challenge.Status.ACTIVE,
            end_date=date.today() + timedelta(days=3),
        )

    def test_scored_lift_qualifies_inside_window(self):
        card = _endgame_scored_card(
            "Squat",
            next_point_gap_fraction=Decimal("0.03"),
            next_point_weight_gap=Decimal("2"),
        )
        _flag_endgame_suggestion([card], self._in_window())
        assert card["endgame_suggestion"] == "next_point"

    def test_unscored_lift_qualifies_weight_path(self):
        card = _close_card("Squat", gap_fraction=Decimal("0.03"))
        _flag_endgame_suggestion([card], self._in_window())
        assert card["endgame_suggestion"] == "first_point"
        assert card["endgame_suggestion_via"] == "weight"

    def test_unscored_lift_qualifies_reps_only_path(self):
        card = _close_card("Squat", gap_fraction=Decimal("0.40"), reps_gap=2)
        _flag_endgame_suggestion([card], self._in_window())
        assert card["endgame_suggestion"] == "first_point"
        assert card["endgame_suggestion_via"] == "reps"

    def test_unscored_lift_qualifies_both_paths(self):
        # gap_fraction under the weight threshold AND reps_gap under the reps
        # threshold: both distances should be recorded, not collapsed to weight.
        card = _close_card("Squat", gap_fraction=Decimal("0.03"), reps_gap=1)
        _flag_endgame_suggestion([card], self._in_window())
        assert card["endgame_suggestion"] == "first_point"
        assert card["endgame_suggestion_via"] == "both"

    def test_nothing_flagged_outside_window(self):
        challenge = Challenge(
            status=Challenge.Status.ACTIVE,
            end_date=date.today() + timedelta(days=40),
        )
        card = _close_card("Squat", gap_fraction=Decimal("0.01"))
        _flag_endgame_suggestion([card], challenge)
        assert "endgame_suggestion" not in card

    def test_nothing_flagged_when_gap_fails_both_thresholds(self):
        card = _close_card("Squat", gap_fraction=Decimal("0.40"), reps_gap=5)
        _flag_endgame_suggestion([card], self._in_window())
        assert "endgame_suggestion" not in card

    def test_smallest_gap_wins_across_scored_and_unscored(self):
        scored = _endgame_scored_card(
            "Bench Press",
            next_point_gap_fraction=Decimal("0.04"),
            next_point_weight_gap=Decimal("3"),
        )
        unscored = _close_card("Squat", gap_fraction=Decimal("0.01"))
        _flag_endgame_suggestion([scored, unscored], self._in_window())
        assert unscored["endgame_suggestion"] == "first_point"
        assert "endgame_suggestion" not in scored

    def test_no_data_before_window_never_qualifies(self):
        card = _close_card(
            "Squat",
            state="no_data_before_window",
            gap_fraction=Decimal("0.01"),
            reps_gap=1,
        )
        _flag_endgame_suggestion([card], self._in_window())
        assert "endgame_suggestion" not in card

    def test_scored_at_max_tier_never_qualifies(self):
        # points_earned == 10 leaves next_point_gap_fraction None (AC #7).
        card = _endgame_scored_card(
            "Squat", points_earned=10, next_point_gap_fraction=None
        )
        _flag_endgame_suggestion([card], self._in_window())
        assert "endgame_suggestion" not in card

    def test_terminal_challenge_inside_window_never_flags(self):
        challenge = Challenge(
            status=Challenge.Status.CANCELLED,
            end_date=date.today() + timedelta(days=3),
        )
        card = _close_card("Squat", gap_fraction=Decimal("0.01"))
        _flag_endgame_suggestion([card], challenge)
        assert "endgame_suggestion" not in card


class TestEndgameSuggestionIntegration:
    """End-to-end endgame suggestion through the detail view (TASK-212)."""

    @pytest.fixture(autouse=True)
    def _pin_thresholds(self, settings):
        settings.CHALLENGES_ENDGAME_WINDOW_DAYS = ENDGAME_WINDOW_DAYS
        settings.CHALLENGES_ENDGAME_GAP_FRACTION = float(ENDGAME_GAP_FRACTION)
        settings.CHALLENGES_ENDGAME_REPS_GAP = ENDGAME_REPS_GAP
        settings.CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION = float(
            CLOSE_TO_GOAL_GAP_FRACTION
        )
        settings.CHALLENGES_CLOSE_TO_GOAL_REPS_GAP = CLOSE_TO_GOAL_REPS_GAP

    def test_scored_lift_gets_next_point_suggestion_inside_window(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        challenge.end_date = date.today() + timedelta(days=3)
        challenge.save(update_fields=["end_date"])
        # one_rm = 1.5 * 100 = 150. A 4-point best sits at reps = 11 - 4 = 7. The
        # next point (5) targets reps = 6: threshold_for_reps(150, 6). Log a set
        # just short of that so the weight gap clears the 5% band.
        one_rm = Decimal("1.5000") * Decimal("100.00")
        next_target = threshold_for_reps(one_rm, 6)
        weight = (next_target - Decimal("1.00")).quantize(Decimal("0.01"))
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=7,
            weight=weight,
            points_earned=4,
            is_current_best=True,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "scored"
        assert squat["next_point_gap_fraction"] == (next_target - weight) / next_target
        assert squat["endgame_suggestion"] == "next_point"
        assert squat["next_point_weight_gap"] is not None
        content = resp.content.decode()
        assert "Final stretch" in content
        assert "away from your next point on Squat" in content

    def test_unscored_card_holds_both_close_to_goal_and_endgame(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # doc-5 collision test: the two features coexist on the same card.
        challenge.end_date = date.today() + timedelta(days=3)
        challenge.save(update_fields=["end_date"])
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=10,
            weight=Decimal("110.00"),
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_points"
        assert squat["close_to_goal"] is True
        assert squat["endgame_suggestion"] == "first_point"
        content = resp.content.decode()
        assert "Close to goal" in content
        assert "Final stretch" in content
        assert "your first point on Squat" in content

    def test_unscored_both_paths_render_both_distances(
        self,
        authed_client,
        participant,
        challenge,
        user,
        squat_multiplier,
        mock_sync,
    ):
        # A set that lands under BOTH the weight-fraction and reps-gap thresholds
        # must restate both distances, not collapse to the weight path.
        # one_rm = 1.5 * 100 = 150. reps=8, weight=115: the weight gap to the
        # 8-rep threshold clears the 5% band, and the load already meets the
        # 10-rep threshold, so reps_gap = 2 (<= the endgame reps threshold).
        challenge.end_date = date.today() + timedelta(days=3)
        challenge.save(update_fields=["end_date"])
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            reps=8,
            weight=Decimal("115.00"),
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        resp = _get(authed_client, challenge)
        squat = next(
            c
            for c in resp.context["personal_data"]["summary_cards"]
            if c["lift"] == "Squat"
        )
        assert squat["state"] == "no_points"
        assert squat["endgame_suggestion"] == "first_point"
        assert squat["endgame_suggestion_via"] == "both"
        content = resp.content.decode()
        assert "away from your first point on Squat" in content
        assert "more rep" in content
        assert "at this weight" in content
