"""Tests for the co-participant chart view (TASK-252)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.goal_builders import standards_source_detail
from challenges.models import Challenge, ChallengeParticipant, CustomGoal
from challenges.services import build_participant_chart
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    CustomGoalFactory,
    CustomGoalTargetFactory,
    make_custom_challenge,
)
from scoring.services import get_user_standing
from scoring.tests.factories import PointEarnEventFactory

pytestmark = pytest.mark.django_db

LIFT = "Bench Press"
HX = {"HTTP_HX_REQUEST": "true"}


def _accept(participant):
    participant.invite_status = ChallengeParticipant.InviteStatus.ACCEPTED
    participant.joined_at = timezone.now() - timedelta(days=30)
    participant.save(update_fields=["invite_status", "joined_at"])
    return participant


def _give_goal(participant, *, name="Goal", targets=None, **goal_kwargs):
    targets = (
        targets
        if targets is not None
        else {rep: Decimal("100.00") for rep in range(1, 11)}
    )
    goal = CustomGoalFactory(participant=participant, name=name, **goal_kwargs)
    for rep, weight in targets.items():
        CustomGoalTargetFactory(
            goal=goal, lift=LIFT, rep_count=rep, target_weight=weight
        )
    participant.custom_goal = goal
    participant.save(update_fields=["custom_goal"])
    return goal


@pytest.fixture
def viewer():
    return UserFactory(unit_preference="kg")


@pytest.fixture
def challenge(viewer):
    return make_custom_challenge(
        lifts=[LIFT], creator=viewer, status=Challenge.Status.ACTIVE
    )


@pytest.fixture
def viewer_participant(viewer, challenge):
    participant = _accept(ChallengeParticipantFactory(user=viewer, challenge=challenge))
    _give_goal(participant, name="Viewer Goal")
    return participant


@pytest.fixture
def subject():
    return UserFactory(unit_preference="kg")


@pytest.fixture
def subject_participant(subject, challenge):
    participant = _accept(
        ChallengeParticipantFactory(user=subject, challenge=challenge)
    )
    _give_goal(participant, name="Subject Goal")
    return participant


@pytest.fixture
def viewer_client(viewer, viewer_participant):
    """An authed client for `viewer`, who is guaranteed to be an accepted
    member of `challenge` — most tests need this to exercise the SUBJECT
    boundary rather than tripping the viewer guard first."""
    client = Client()
    client.force_login(viewer)
    return client


def chart_url(challenge, participant):
    return reverse("challenges:participant-chart", args=[challenge.pk, participant.pk])


class TestAuthorization:
    def test_non_participant_gets_403(self, challenge, subject_participant):
        outsider = UserFactory()
        client = Client()
        client.force_login(outsider)
        resp = client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 403

    def test_invited_only_viewer_gets_403(self, challenge, subject_participant):
        invited_user = UserFactory()
        ChallengeParticipantFactory(
            user=invited_user,
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        client = Client()
        client.force_login(invited_user)
        resp = client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 403

    def test_declined_viewer_gets_403(self, challenge, subject_participant):
        declined_user = UserFactory()
        ChallengeParticipantFactory(
            user=declined_user,
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.DECLINED,
        )
        client = Client()
        client.force_login(declined_user)
        resp = client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 403

    def test_bailed_viewer_gets_403(
        self, challenge, viewer, viewer_participant, subject_participant
    ):
        """A departed viewer is denied unconditionally: the OPEN-challenge
        read-access carve-out retired with open visibility itself (TASK-272)."""
        viewer_participant.is_bailed = True
        viewer_participant.bailed_at = timezone.now()
        viewer_participant.save(update_fields=["is_bailed", "bailed_at"])
        client = Client()
        client.force_login(viewer)
        resp = client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 403

    def test_subject_in_different_challenge_gets_404(
        self, viewer_client, challenge, viewer
    ):
        other_challenge = make_custom_challenge(lifts=[LIFT], creator=viewer)
        other_subject_user = UserFactory()
        other_subject = _accept(
            ChallengeParticipantFactory(
                user=other_subject_user, challenge=other_challenge
            )
        )
        _give_goal(other_subject)
        resp = viewer_client.get(chart_url(challenge, other_subject))
        assert resp.status_code == 404

    def test_bailed_subject_gets_404(
        self, viewer_client, challenge, subject_participant
    ):
        subject_participant.is_bailed = True
        subject_participant.bailed_at = timezone.now()
        subject_participant.save(update_fields=["is_bailed", "bailed_at"])
        resp = viewer_client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 404

    def test_non_accepted_subject_gets_404(self, viewer_client, challenge, subject):
        subject_participant = ChallengeParticipantFactory(
            user=subject,
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        resp = viewer_client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 404

    def test_deactivated_subject_gets_404(
        self, viewer_client, challenge, subject, subject_participant
    ):
        subject.is_active = False
        subject.save(update_fields=["is_active"])
        resp = viewer_client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 404

    def test_viewer_can_view_own_chart(
        self, viewer_client, challenge, viewer_participant
    ):
        resp = viewer_client.get(chart_url(challenge, viewer_participant))
        assert resp.status_code == 200


class TestRendering:
    def test_htmx_request_renders_partial(
        self, viewer_client, challenge, subject_participant
    ):
        resp = viewer_client.get(chart_url(challenge, subject_participant), **HX)
        assert resp.status_code == 200
        names = [t.name for t in resp.templates]
        assert "challenges/_participant_chart.html" in names
        assert "challenges/participant_chart.html" not in names

    def test_plain_get_renders_standalone_page(
        self, viewer_client, challenge, subject_participant
    ):
        resp = viewer_client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 200
        names = [t.name for t in resp.templates]
        assert "challenges/participant_chart.html" in names
        assert "challenges/_participant_chart.html" in names

    def test_every_lift_and_all_rep_columns_appear(
        self, viewer_client, challenge, subject, subject_participant
    ):
        second_lift = "Squat"
        challenge.custom_lifts.create(name=second_lift)
        goal = subject_participant.custom_goal
        for rep in range(1, 11):
            CustomGoalTargetFactory(
                goal=goal,
                lift=second_lift,
                rep_count=rep,
                target_weight=Decimal("150.00"),
            )
        resp = viewer_client.get(chart_url(challenge, subject_participant))
        content = resp.content.decode()
        assert LIFT in content
        assert second_lift in content
        # Columns lead with what they're worth; the rep count is the
        # sub-label ("10pt" over "1 reps"), so both ends must be present.
        assert "10pt" in content and "1pt" in content

    def test_highlighted_cell_matches_current_best(
        self, viewer_client, viewer, challenge, subject, subject_participant
    ):
        reps = 7
        points_earned = 11 - reps
        PointEarnEventFactory(
            user=subject,
            challenge=challenge,
            lift=LIFT,
            reps=reps,
            weight=Decimal("100.00"),
            points_earned=points_earned,
            is_current_best=True,
        )
        resp = viewer_client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 200

        chart = build_participant_chart(viewer, challenge, subject_participant)
        row = next(r for r in chart["standards_rows"] if r["lift"] == LIFT)
        highlighted = [c for c in row["cells"] if c["is_current_best"]]
        assert len(highlighted) == 1
        assert highlighted[0]["reps"] == reps
        point_row = next(r for r in chart["point_rows"] if r["lift"] == LIFT)
        assert point_row["points_earned"] == points_earned

    def test_total_points_matches_leaderboard_standing(
        self, viewer_client, viewer, challenge, subject, subject_participant
    ):
        PointEarnEventFactory(
            user=subject,
            challenge=challenge,
            lift=LIFT,
            reps=5,
            weight=Decimal("100.00"),
            points_earned=6,
            is_current_best=True,
        )
        resp = viewer_client.get(chart_url(challenge, subject_participant))
        assert resp.status_code == 200

        chart = build_participant_chart(viewer, challenge, subject_participant)
        standing = get_user_standing(challenge, subject)
        assert chart["total_points"] == standing["total_points"]

    def test_subject_without_goal_shows_hasnt_set_goal_copy(
        self, viewer_client, challenge, subject
    ):
        goalless_participant = _accept(
            ChallengeParticipantFactory(user=subject, challenge=challenge)
        )
        resp = viewer_client.get(chart_url(challenge, goalless_participant))
        assert resp.status_code == 200
        assert "hasn&#x27;t set their goal yet" in resp.content.decode() or (
            "hasn't set their goal yet" in resp.content.decode()
        )

    def test_unit_preference_uses_viewer_not_subject(self, challenge, subject):
        """C4 regression: display_unit must be the VIEWER's preference, not
        the subject's — build_personal_data has this exact bug shape today
        (challenges/services.py:1008 reads user.unit_preference where `user`
        is the subject), and TASK-252's plan calls it the single most likely
        thing to get wrong when reusing that code for a peer view."""
        lb_viewer = UserFactory(unit_preference="lb")
        viewer_participant = _accept(
            ChallengeParticipantFactory(user=lb_viewer, challenge=challenge)
        )
        _give_goal(viewer_participant, name="LB Viewer Goal")

        kg_subject_participant = _accept(
            ChallengeParticipantFactory(user=subject, challenge=challenge)
        )
        _give_goal(
            kg_subject_participant,
            name="KG Subject Goal",
            targets={rep: Decimal("100.00") for rep in range(1, 11)},
        )

        client = Client()
        client.force_login(lb_viewer)
        resp = client.get(chart_url(challenge, kg_subject_participant))

        chart = build_participant_chart(lb_viewer, challenge, kg_subject_participant)
        assert chart["display_unit"] == "lb"
        row = next(r for r in chart["standards_rows"] if r["lift"] == LIFT)
        # 100 kg ~= 220.5 lb — the grid must show a pound figure, not 100.
        ten_rm_cell = next(c for c in row["cells"] if c["reps"] == 10)
        assert ten_rm_cell["weight"] != Decimal("100.00")
        assert ten_rm_cell["weight"] > Decimal("200.0")
        assert resp.status_code == 200


class TestProvenance:
    def test_standards_provenance_withholds_bodyweight_sex_tier_population(
        self, viewer_client, challenge, subject, subject_participant
    ):
        source_detail = standards_source_detail(
            population="general",
            snapshot_version="v-distinctive-2026-07",
            tier="elite-distinctive-tier",
            sex="M",
            bodyweight_kg=Decimal("83.33"),
        )
        subject_participant.custom_goal.source_method = (
            CustomGoal.SourceMethod.STANDARDS
        )
        subject_participant.custom_goal.source_detail = source_detail
        subject_participant.custom_goal.save(
            update_fields=["source_method", "source_detail"]
        )

        resp = viewer_client.get(chart_url(challenge, subject_participant))
        body = resp.content.decode()

        assert "83.33" not in body
        assert "elite-distinctive-tier" not in body
        assert "general" not in body
        assert ">M<" not in body
        assert "v-distinctive-2026-07" in body
        assert "FitnessVolt" in body

    def test_history_provenance_shows_uplift_and_lookback(
        self, viewer_client, challenge, subject_participant
    ):
        subject_participant.custom_goal.source_method = CustomGoal.SourceMethod.HISTORY
        subject_participant.custom_goal.source_detail = {
            "uplift": 0.15,
            "lookback_days": 45,
            "rounding_amount": None,
            "rounding_unit": None,
        }
        subject_participant.custom_goal.save(
            update_fields=["source_method", "source_detail"]
        )

        resp = viewer_client.get(chart_url(challenge, subject_participant))
        body = resp.content.decode()
        assert "15%" in body
        assert "45" in body

    def test_history_provenance_shows_rounding_when_present(
        self, viewer_client, challenge, subject_participant
    ):
        subject_participant.custom_goal.source_method = CustomGoal.SourceMethod.HISTORY
        subject_participant.custom_goal.source_detail = {
            "uplift": 0.1,
            "lookback_days": 30,
            "rounding_amount": "2.5",
            "rounding_unit": "kg",
        }
        subject_participant.custom_goal.save(
            update_fields=["source_method", "source_detail"]
        )

        resp = viewer_client.get(chart_url(challenge, subject_participant))
        body = resp.content.decode()
        assert "2.5" in body


class TestDetailPageLink:
    def test_leaderboard_links_to_active_co_participant_chart(
        self, viewer_client, challenge, subject, subject_participant
    ):
        PointEarnEventFactory(
            user=subject,
            challenge=challenge,
            lift=LIFT,
            reps=5,
            weight=Decimal("100.00"),
            points_earned=6,
            is_current_best=True,
        )
        resp = viewer_client.get(reverse("challenges:detail", args=[challenge.pk]))
        body = resp.content.decode()
        expected_url = chart_url(challenge, subject_participant)
        assert f'hx-get="{expected_url}"' in body

    def test_deactivated_participant_row_has_no_link(
        self, viewer_client, challenge, subject, subject_participant
    ):
        PointEarnEventFactory(
            user=subject,
            challenge=challenge,
            lift=LIFT,
            reps=5,
            weight=Decimal("100.00"),
            points_earned=6,
            is_current_best=True,
        )
        subject.is_active = False
        subject.save(update_fields=["is_active"])
        resp = viewer_client.get(reverse("challenges:detail", args=[challenge.pk]))
        body = resp.content.decode()
        assert subject.effective_display_name in body
        assert chart_url(challenge, subject_participant) not in body

    def test_own_row_has_no_chart_link(
        self,
        viewer_client,
        challenge,
        viewer,
        subject,
        viewer_participant,
        subject_participant,
    ):
        # Create scored events for both viewer and subject so both appear in leaderboard
        PointEarnEventFactory(
            user=viewer,
            challenge=challenge,
            lift=LIFT,
            reps=5,
            weight=Decimal("100.00"),
            points_earned=6,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=subject,
            challenge=challenge,
            lift=LIFT,
            reps=5,
            weight=Decimal("110.00"),
            points_earned=7,
            is_current_best=True,
        )
        resp = viewer_client.get(reverse("challenges:detail", args=[challenge.pk]))
        body = resp.content.decode()
        # Viewer's own row should not have a chart link
        viewer_chart_url = chart_url(challenge, viewer_participant)
        assert f'hx-get="{viewer_chart_url}"' not in body
        # Subject's row should still have a chart link
        subject_chart_url = chart_url(challenge, subject_participant)
        assert f'hx-get="{subject_chart_url}"' in body
