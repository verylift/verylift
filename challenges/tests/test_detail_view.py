"""Tests for the challenge detail view sync trigger (TASK-25)."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from accounts.services import anonymize_account
from accounts.tests.factories import UserFactory
from challenges.custom_goals import save_custom_goal
from challenges.models import Challenge, ChallengeParticipant
from challenges.rep_target_goals import save_rep_target_goal
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
    CustomGoalFactory,
    RepTargetGoalFactory,
    make_custom_challenge,
    make_rep_target_challenge,
)
from liftosaur.models import LiftosaurSyncLog
from liftosaur.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent
from scoring.tests.factories import PointEarnEventFactory


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def challenge(db, user):
    return ChallengeFactory(creator=user, status=Challenge.Status.ACTIVE)


@pytest.fixture
def participant(challenge, user):
    participant = ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )
    goal = CustomGoalFactory(participant=participant, name="Intermediate")
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
    """Patch the two decoupled steps the detail view composes.

    Yields a namespace of the pull (sync_user_lifts) and score
    (score_pooled_history) mocks so tests can assert the composition without
    hitting Liftosaur or mutating pre-seeded PointEarnEvents.
    """
    with (
        patch("challenges.services.sync_user_lifts") as mock_pull,
        patch("challenges.services.score_pooled_history") as mock_score,
    ):
        yield SimpleNamespace(pull=mock_pull, score=mock_score)


class TestSyncTrigger:
    def test_visiting_detail_syncs_requesting_user(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        assert resp.status_code == 200
        assert challenge.name.encode() in resp.content
        pulled = [call.args[0] for call in mock_sync.pull.call_args_list]
        scored = [call.kwargs["user"] for call in mock_sync.score.call_args_list]
        assert user in pulled
        assert user in scored

    def test_syncs_other_active_participants(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        other = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=other,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        authed_client.get(url)
        pulled = {call.args[0] for call in mock_sync.pull.call_args_list}
        scored = {call.kwargs["user"] for call in mock_sync.score.call_args_list}
        assert {user, other} <= pulled
        assert {user, other} <= scored

    def test_skips_bailed_and_invited_participants(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        bailed = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=bailed,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        invited = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=invited,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        authed_client.get(url)
        touched = {call.args[0] for call in mock_sync.pull.call_args_list} | {
            call.kwargs["user"] for call in mock_sync.score.call_args_list
        }
        assert bailed not in touched
        assert invited not in touched


class TestParticipantSyncBudget:
    """TASK-320: a challenge with many not-recently-synced participants must
    not let per-participant sync latency scale unbounded with headcount --
    once the wall-clock budget is used up, remaining participants are scored
    without a fresh pull rather than adding another API round-trip."""

    def test_budget_exhaustion_skips_sync_for_later_participants(
        self, authed_client, participant, challenge, user
    ):
        other = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=other,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        url = reverse("challenges:detail", args=[challenge.pk])

        # One monotonic() call computes the deadline, then one call per
        # participant checks against it: the first participant is still
        # inside the 30s budget, the second lands after it's exhausted.
        with (
            patch("challenges.views.time.monotonic", side_effect=[0, 5, 35]),
            patch("challenges.views.sync_and_score") as mock_sync_and_score,
        ):
            resp = authed_client.get(url)

        assert resp.status_code == 200
        calls_by_user = {
            call.args[0]: call.kwargs["sync"]
            for call in mock_sync_and_score.call_args_list
        }
        assert calls_by_user[user] is True
        assert calls_by_user[other] is False

    def test_within_budget_all_participants_synced(
        self, authed_client, participant, challenge, user
    ):
        other = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=other,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        url = reverse("challenges:detail", args=[challenge.pk])

        with patch("challenges.views.sync_and_score") as mock_sync_and_score:
            authed_client.get(url)

        calls_by_user = {
            call.args[0]: call.kwargs["sync"]
            for call in mock_sync_and_score.call_args_list
        }
        assert calls_by_user[user] is True
        assert calls_by_user[other] is True

    def test_one_participants_failing_sync_does_not_break_the_shared_page(
        self, authed_client, participant, challenge, user, caplog
    ):
        """The detail page is shared by the whole challenge, so a single
        member's tracker blowing up must not 500 it for everyone -- the rest of
        the field still gets scored from whatever is already pooled."""
        other = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=other,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        url = reverse("challenges:detail", args=[challenge.pk])

        def explode_for_the_first_participant(participant_user, _challenge, **kwargs):
            if participant_user == user:
                raise RuntimeError("self-hosted tracker fell over")

        with (
            caplog.at_level(logging.ERROR, logger="challenges.views"),
            patch(
                "challenges.views.sync_and_score",
                side_effect=explode_for_the_first_participant,
            ) as mock_sync_and_score,
        ):
            resp = authed_client.get(url)

        assert resp.status_code == 200
        # The loop kept going rather than aborting on the first raise.
        assert other in {call.args[0] for call in mock_sync_and_score.call_args_list}
        assert str(user.id) in caplog.text


@pytest.mark.django_db
class TestGreedyScoringOnDetailOpen:
    """Scoring runs on every detail open, decoupled from the API cooldown."""

    def _setup_scorable(self, user, challenge):
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=datetime.now(tz=UTC) - timedelta(days=30),
        )
        save_custom_goal(
            participant,
            "Intermediate",
            {"Back Squat": {rep: Decimal("100.00") for rep in range(1, 11)}},
        )
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=datetime.now(tz=UTC).date(),
            reps=1,
            weight_kg=Decimal("120.00"),
        )
        return participant

    def test_scores_pool_even_when_pull_is_cooldown_skipped(self):
        user = UserFactory(liftosaur_api_key="key")
        challenge = make_custom_challenge(
            lifts=["Back Squat"],
            creator=user,
            status=Challenge.Status.ACTIVE,
            end_date=(datetime.now(tz=UTC) + timedelta(days=30)).date(),
        )
        self._setup_scorable(user, challenge)
        # A recent successful pull puts the user inside the cooldown window, so
        # sync_user_lifts must short-circuit the API pull.
        LiftosaurSyncLog.objects.create(
            user=user,
            started_at=datetime.now(tz=UTC) - timedelta(minutes=1),
            completed_at=datetime.now(tz=UTC) - timedelta(minutes=1),
            success=True,
        )

        client = Client()
        client.force_login(user)
        url = reverse("challenges:detail", args=[challenge.pk])
        with patch("liftosaur.services.LiftosaurClient") as mock_client_cls:
            resp = client.get(url)

        assert resp.status_code == 200
        # Pull was cooldown-skipped: no API client built, no new sync log.
        mock_client_cls.assert_not_called()
        assert LiftosaurSyncLog.objects.filter(user=user).count() == 1
        # Scoring still ran greedily: the pooled set produced a point event.
        event = PointEarnEvent.objects.get(user=user, challenge=challenge)
        assert event.is_current_best is True
        assert event.points_earned == 10

    def test_login_required(self, db, challenge):
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = Client().get(url)
        assert resp.status_code == 302
        assert "/login" in resp.url or "next=" in resp.url

    def test_unknown_challenge_404(self, authed_client):
        import uuid

        url = reverse("challenges:detail", args=[uuid.uuid4()])
        resp = authed_client.get(url)
        assert resp.status_code == 404


class TestAccessControl:
    def test_non_participant_403(self, db, challenge, mock_sync):
        outsider = UserFactory()
        client = Client()
        client.force_login(outsider)
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = client.get(url)
        assert resp.status_code == 403

    def test_invited_but_not_accepted_403(self, db, challenge, mock_sync):
        invitee = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=invitee,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        client = Client()
        client.force_login(invitee)
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = client.get(url)
        assert resp.status_code == 403

    def test_declined_403(self, db, challenge, mock_sync):
        decliner = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=decliner,
            invite_status=ChallengeParticipant.InviteStatus.DECLINED,
        )
        client = Client()
        client.force_login(decliner)
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = client.get(url)
        assert resp.status_code == 403

    def test_accepted_participant_allowed(
        self, authed_client, participant, challenge, mock_sync
    ):
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        assert resp.status_code == 200

    def test_voluntary_bail_403(self, db, mock_sync):
        """Departed access is denied unconditionally now: OPEN challenges used
        to keep read access, and that carve-out retired with open visibility
        itself (TASK-272)."""
        user = UserFactory()
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        client = Client()
        client.force_login(user)
        url = reverse("challenges:detail", args=[challenge.pk])
        assert client.get(url).status_code == 403

    def test_creator_removal_403(self, db, mock_sync):
        user = UserFactory()
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
            removed_by_creator=True,
        )
        client = Client()
        client.force_login(user)
        url = reverse("challenges:detail", args=[challenge.pk])
        assert client.get(url).status_code == 403


class TestHeader:
    def test_header_renders_challenge_metadata(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        # The `participant` fixture's goal is already named "Intermediate".
        user.display_name = "Big Lifter"
        user.save(update_fields=["display_name"])
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        content = resp.content.decode()
        assert challenge.name in content
        assert "Big Lifter" in content
        assert "Intermediate" in content
        assert "Active" in content

    def test_rep_target_goal_shown_in_header(self, db, mock_sync):
        # Regression: the header read only participant.custom_goal, so every
        # Rep Target participant with a locked goal saw "Not set" above their
        # own rendered goal table.
        challenge = make_rep_target_challenge(
            lifts=["Push Up"], status=Challenge.Status.ACTIVE
        )
        user = UserFactory()
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        goal = RepTargetGoalFactory(participant=participant, name="Summer Reps")
        participant.rep_target_goal = goal
        participant.save(update_fields=["rep_target_goal"])
        client = Client()
        client.force_login(user)
        resp = client.get(reverse("challenges:detail", args=[challenge.pk]))
        content = resp.content.decode()
        assert "Summer Reps" in content
        assert "Not set" not in content


class TestLeaderboard:
    def test_leaderboard_order_and_ties(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        low = UserFactory(display_name="Low Scorer")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=low,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        tie = UserFactory(display_name="Tie Scorer")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=tie,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        PointEarnEventFactory(
            user=user, challenge=challenge, lift="Squat", points_earned=10
        )
        PointEarnEventFactory(
            user=low, challenge=challenge, lift="Squat", points_earned=3
        )
        PointEarnEventFactory(
            user=tie, challenge=challenge, lift="Squat", points_earned=10
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        leaderboard = resp.context["leaderboard"]
        ranks = {row["name"]: row["rank"] for row in leaderboard}
        points = {row["name"]: row["total_points"] for row in leaderboard}
        # user and tie both have 10 -> rank 1; low has 3 -> rank 2
        assert ranks["Low Scorer"] == 2
        assert points["Low Scorer"] == 3
        assert all(r == 1 for n, r in ranks.items() if n != "Low Scorer")
        # ordered descending: rank-1 rows precede rank-2 row
        assert leaderboard[-1]["name"] == "Low Scorer"

    def test_self_row_flagged(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        PointEarnEventFactory(
            user=user, challenge=challenge, lift="Squat", points_earned=5
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        self_rows = [r for r in resp.context["leaderboard"] if r["is_self"]]
        assert len(self_rows) == 1
        assert "(you)" in resp.content.decode()

    def test_deactivated_user_excluded_and_ranks_recompute(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        """A deleted account occupies no leaderboard row, exactly like a bailed
        participant -- and its pseudonym must not be reachable in the rendered
        page under any name. The rank assertion is the load-bearing half: it
        proves the row was dropped before dense-ranking rather than merely
        hidden in the template, which is what lets the survivors close up
        over it."""
        gone = UserFactory(display_name="Gone User", is_active=False)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=gone,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        PointEarnEventFactory(
            user=gone, challenge=challenge, lift="Squat", points_earned=20
        )
        PointEarnEventFactory(
            user=user, challenge=challenge, lift="Squat", points_earned=5
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        content = resp.content.decode()
        names = {row["name"] for row in resp.context["leaderboard"]}
        assert "Gone User" not in names
        assert "Gone User" not in content
        survivor = next(r for r in resp.context["leaderboard"] if r["is_self"])
        assert survivor["rank"] == 1

    def test_anonymized_account_not_shown_under_its_pseudonym(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        """Driven through the real anonymize_account rather than a hand-set
        is_active=False, so the exclusion is pinned to what account deletion
        actually writes. The pseudonym is a plausible human name, so the
        assertion is that this specific generated one never reaches the page.
        """
        gone = UserFactory(display_name="Gone User")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=gone,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        PointEarnEventFactory(
            user=gone, challenge=challenge, lift="Squat", points_earned=7
        )
        anonymize_account(gone)

        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        content = resp.content.decode()
        assert gone.display_name not in content
        assert "(deleted)" not in content

    def test_bailed_participant_excluded_and_ranks_recompute(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        bailer = UserFactory(display_name="Left Lifter")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=bailer,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        PointEarnEventFactory(
            user=bailer, challenge=challenge, lift="Squat", points_earned=20
        )
        PointEarnEventFactory(
            user=user, challenge=challenge, lift="Squat", points_earned=5
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        leaderboard = resp.context["leaderboard"]
        names = {row["name"] for row in leaderboard}
        assert "Left Lifter" not in names
        assert "Left Lifter" not in resp.content.decode()
        # the surviving lifter takes rank 1 despite the bailer's higher points
        survivor = next(row for row in leaderboard if row["is_self"])
        assert survivor["rank"] == 1

    def test_unscored_participant_shown_with_zero_points(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        """A participant with no PointEarnEvent still appears on the leaderboard
        (TASK-304), instead of being invisible."""
        never_scored = UserFactory(display_name="Never Scored")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=never_scored,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        PointEarnEventFactory(
            user=user, challenge=challenge, lift="Squat", points_earned=5
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        leaderboard = resp.context["leaderboard"]
        row = next(r for r in leaderboard if r["name"] == "Never Scored")
        assert row["total_points"] == 0

    def test_no_scores_at_all_shows_dash_ranks(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        """When nobody in the challenge has scored anything yet, every rank
        renders as '-' rather than a numeric dense rank (TASK-304)."""
        other = UserFactory(display_name="Other Lifter")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=other,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        leaderboard = resp.context["leaderboard"]
        assert len(leaderboard) == 2
        assert all(row["rank"] == "-" for row in leaderboard)


class TestChart:
    def test_chart_data_in_context(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        user.display_name = "Charter"
        user.save(update_fields=["display_name"])
        PointEarnEventFactory(
            user=user, challenge=challenge, lift="Squat", points_earned=9
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        chart_data = resp.context["chart_data"]
        labels = [ds["label"] for ds in chart_data["datasets"]]
        assert "Charter" in labels

    def test_chart_renders_canvas_and_vendor_script(
        self, authed_client, participant, challenge, mock_sync
    ):
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        content = resp.content.decode()
        assert 'id="points-over-time-chart"' in content
        assert "/static/vendor/chart.js" in content
        assert "cdn.jsdelivr.net" not in content


class TestLastSyncedStamp:
    """Detail page surfaces the user's last successful sync time (TASK-144)."""

    def test_stamp_rendered_when_successful_sync_exists(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        from django.utils import timezone

        from liftosaur.tests.factories import LiftosaurSyncLogFactory

        LiftosaurSyncLogFactory(user=user, success=True, started_at=timezone.now())
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        assert resp.status_code == 200
        assert b"Last synced" in resp.content

    def test_no_stamp_when_never_synced(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        assert resp.status_code == 200
        assert b"Last synced" not in resp.content


class TestLeaveChallengeLink:
    """Leave-challenge link renders for eligible participants (TASK-151)."""

    def test_eligible_participant_sees_leave_link(
        self, authed_client, participant, challenge, mock_sync
    ):
        """Accepted, non-bailed participant on active challenge sees leave link."""
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        assert resp.status_code == 200
        bail_url = reverse("challenges:bail", args=[challenge.pk])
        assert bail_url.encode() in resp.content

    def test_bailed_participant_cannot_reach_the_page_at_all(
        self, authed_client, participant, challenge, mock_sync
    ):
        """The leave link is moot for a bailed participant: since TASK-272 they
        are denied the detail page outright, so the template's own
        `not participant.is_bailed` guard is belt-and-braces."""
        participant.is_bailed = True
        participant.save(update_fields=["is_bailed"])
        url = reverse("challenges:detail", args=[challenge.pk])
        assert authed_client.get(url).status_code == 403

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_terminal_challenge_hides_leave_link(
        self, authed_client, participant, challenge, mock_sync, status
    ):
        """Both terminal statuses hide the link, matching bail_view's guard --
        a cancelled challenge is as read-only as a completed one, and bailing
        from one would still detach the participant's locked goal."""
        challenge.status = status
        challenge.save(update_fields=["status"])
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        assert resp.status_code == 200
        bail_url = reverse("challenges:bail", args=[challenge.pk])
        assert bail_url.encode() not in resp.content


class TestOthersQuerySelectRelated:
    """Verify that the others queryset uses select_related('user') to avoid N+1."""

    @pytest.mark.django_db
    def test_detail_view_resolves_other_users_without_n_plus_one(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        """The others queryset uses select_related('user'), so accessing other.user
        for multiple participants does not issue per-participant queries."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        # Add a few other participants.
        other1 = UserFactory(display_name="Other One")
        other2 = UserFactory(display_name="Other Two")
        other3 = UserFactory(display_name="Other Three")
        for other in [other1, other2, other3]:
            ChallengeParticipantFactory(
                challenge=challenge,
                user=other,
                invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            )

        url = reverse("challenges:detail", args=[challenge.pk])

        # Capture queries during the GET.
        with CaptureQueriesContext(connection) as queries:
            resp = authed_client.get(url)

        assert resp.status_code == 200

        # Count how many SELECT queries touch the auth_user table after
        # fetching the challenge. The select_related('user') on the others
        # queryset means we fetch users in one query, not per-participant
        # queries.
        user_queries = [
            q for q in queries if "SELECT" in q["sql"] and "auth_user" in q["sql"]
        ]

        # We expect a bounded number of user queries (typically 1-2 from
        # select_related), not 4+ (one per other participant).
        assert len(user_queries) <= 3, (
            f"Expected at most 3 user queries (including requesting user "
            f"lookup), but found {len(user_queries)}: "
            f"{[q['sql'][:80] for q in user_queries]}"
        )


class TestMobileHeaderTitle:
    """Mobile header shows challenge name on detail page (TASK-216)."""

    def test_detail_page_shows_challenge_name_in_mobile_header(
        self, authed_client, participant, challenge, mock_sync
    ):
        """Detail page sets mobile_header_title to challenge name."""
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        assert resp.status_code == 200
        assert resp.context["mobile_header_title"] == challenge.name

    def test_dashboard_page_does_not_set_mobile_header_title(
        self, authed_client, participant, mock_sync
    ):
        """Dashboard and other non-detail pages should not set mobile_header_title."""
        url = reverse("challenges:dashboard")
        resp = authed_client.get(url)
        assert resp.status_code == 200
        # mobile_header_title should not be in context, or should be falsy
        assert not resp.context.get("mobile_header_title")

    def test_challenge_name_with_long_text_renders(
        self, authed_client, participant, challenge, user, mock_sync
    ):
        """Long names truncate gracefully in mobile header."""
        long_name = (
            "This is a very long challenge name that should truncate "
            "at narrow viewport widths"
        )
        challenge.name = long_name
        challenge.save(update_fields=["name"])
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = authed_client.get(url)
        assert resp.status_code == 200
        assert resp.context["mobile_header_title"] == challenge.name
        # Verify the long name is in the rendered content
        assert challenge.name.encode() in resp.content


class TestGoalSetupGuard:
    """Accepted participants without a configured goal are redirected to goal
    setup instead of viewing an incomplete detail page (TASK-162)."""

    def test_custom_participant_without_goal_redirected(self, db, mock_sync):
        user = UserFactory()
        challenge = make_custom_challenge(creator=user, status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = client.get(url)
        assert resp.status_code == 302
        assert resp.url == reverse("challenges:goal-setup", args=[challenge.pk])
        # Guard sits before the sync/score loop, so neither ran.
        mock_sync.pull.assert_not_called()
        mock_sync.score.assert_not_called()

    def test_custom_participant_with_goal_renders(self, db, mock_sync):
        user = UserFactory()
        challenge = make_custom_challenge(creator=user, status=Challenge.Status.ACTIVE)
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        goal = CustomGoalFactory(participant=participant, name="My Targets")
        participant.custom_goal = goal
        participant.save(update_fields=["custom_goal"])
        client = Client()
        client.force_login(user)
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = client.get(url)
        assert resp.status_code == 200

    def test_bailed_participant_without_goal_denied_before_the_guard(
        self, db, mock_sync
    ):
        """The goal-setup redirect can never fire for a bailed participant since
        TASK-272 — the membership guard denies them first."""
        user = UserFactory()
        challenge = ChallengeFactory(creator=user, status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        client = Client()
        client.force_login(user)
        url = reverse("challenges:detail", args=[challenge.pk])
        assert client.get(url).status_code == 403

    def test_no_goal_in_completed_challenge_not_redirected(self, db, mock_sync):
        user = UserFactory()
        challenge = ChallengeFactory(creator=user, status=Challenge.Status.COMPLETED)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        client = Client()
        client.force_login(user)
        url = reverse("challenges:detail", args=[challenge.pk])
        resp = client.get(url)
        assert resp.status_code == 200


class TestSelfReportControlsOnTerminalChallenge:
    """A finished challenge's "Your Performance" cards are fully read-only.

    Asserted through the self-report POST route the card wires up
    (challenges:manual-lift) rather than through prose or button labels: a
    rendered hx-post to that endpoint IS the control, so its presence or
    absence is the behaviour under test. The flip-card class goes with it --
    .flip-card-front is absolutely positioned and .flip-card is
    overflow:hidden, so a card left carrying those classes with no back face
    (and therefore no JS to size .flip-card-inner) renders as an empty box.
    """

    def _setup(self, status):
        user = UserFactory()
        challenge = make_custom_challenge(
            lifts=["Back Squat"], creator=user, status=status
        )
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=datetime.now(tz=UTC) - timedelta(days=30),
        )
        save_custom_goal(
            participant,
            "Intermediate",
            {"Back Squat": {rep: Decimal("100.00") for rep in range(1, 11)}},
        )
        client = Client()
        client.force_login(user)
        return client, challenge

    def test_active_challenge_renders_the_self_report_form(self, db, mock_sync):
        client, challenge = self._setup(Challenge.Status.ACTIVE)
        resp = client.get(reverse("challenges:detail", args=[challenge.pk]))
        content = resp.content.decode()
        assert reverse("challenges:manual-lift", args=[challenge.pk]) in content
        assert "flip-card" in content

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_terminal_challenge_renders_no_self_report_form(
        self, db, mock_sync, status
    ):
        client, challenge = self._setup(status)
        resp = client.get(reverse("challenges:detail", args=[challenge.pk]))
        content = resp.content.decode()
        # The card itself still renders -- only its self-report half is gone.
        assert "summary-card-back-squat" in content
        assert reverse("challenges:manual-lift", args=[challenge.pk]) not in content
        assert "flip-card" not in content


class TestRepTargetSelfReportControlsOnTerminalChallenge:
    """The REP_TARGET sibling of the class above."""

    def _setup(self, status):
        user = UserFactory()
        challenge = make_rep_target_challenge(
            lifts=["Push Up"], creator=user, status=status
        )
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=datetime.now(tz=UTC) - timedelta(days=30),
        )
        save_rep_target_goal(
            participant,
            "Targets",
            {"Push Up": (Decimal("0.00"), 20)},
        )
        client = Client()
        client.force_login(user)
        return client, challenge

    def test_active_challenge_renders_the_self_report_form(self, db, mock_sync):
        client, challenge = self._setup(Challenge.Status.ACTIVE)
        resp = client.get(reverse("challenges:detail", args=[challenge.pk]))
        content = resp.content.decode()
        assert reverse("challenges:manual-rep-target", args=[challenge.pk]) in content
        assert "flip-card" in content

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_terminal_challenge_renders_no_self_report_form(
        self, db, mock_sync, status
    ):
        client, challenge = self._setup(status)
        resp = client.get(reverse("challenges:detail", args=[challenge.pk]))
        content = resp.content.decode()
        assert "summary-card-push-up" in content
        assert (
            reverse("challenges:manual-rep-target", args=[challenge.pk]) not in content
        )
        assert "flip-card" not in content
