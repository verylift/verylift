"""Tests for the creator-only Challenge Settings page (TASK-183)."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
    CustomGoalFactory,
)

InviteStatus = ChallengeParticipant.InviteStatus


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE)


@pytest.fixture
def creator_client(challenge):
    c = Client()
    c.force_login(challenge.creator)
    return c


@pytest.fixture
def mock_sync():
    """Patch the detail view's decoupled sync/score steps (no Liftosaur calls)."""
    with (
        patch("challenges.services.sync_user_lifts") as mock_pull,
        patch("challenges.services.score_pooled_history") as mock_score,
    ):
        yield SimpleNamespace(pull=mock_pull, score=mock_score)


class TestChallengeSettingsViewAccess:
    def test_creator_gets_200(self, creator_client, challenge):
        url = reverse("challenges:settings", args=[challenge.pk])
        response = creator_client.get(url)
        assert response.status_code == 200

    def test_non_creator_gets_403(self, challenge):
        other = UserFactory()
        c = Client()
        c.force_login(other)
        url = reverse("challenges:settings", args=[challenge.pk])
        response = c.get(url)
        assert response.status_code == 403

    def test_staff_non_creator_gets_200(self, challenge):
        """Staff moderators can reach settings to rescue a challenge."""
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)
        url = reverse("challenges:settings", args=[challenge.pk])
        response = c.get(url)
        assert response.status_code == 200

    def test_staff_non_creator_can_reach_cancel_from_settings(self, challenge):
        """Staff see the Cancel action on settings and can reach the cancel flow."""
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)

        settings_response = c.get(reverse("challenges:settings", args=[challenge.pk]))
        cancel_url = reverse("challenges:cancel", args=[challenge.pk]).encode()
        assert cancel_url in settings_response.content

        # And the cancel confirm page actually loads for staff.
        assert c.get(cancel_url.decode()).status_code == 200

    def test_staff_only_sees_staff_allowed_controls(self, challenge, mock_sync):
        """Staff see Cancel/Remove/Transfer (allow_staff=True) but NOT Close
        Early or the invite-link regenerate form — those action views have no
        staff override, so rendering them would produce buttons that 403 on
        click."""
        member = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        )
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)

        content = c.get(
            reverse("challenges:settings", args=[challenge.pk])
        ).content.decode()

        # Staff-allowed controls are present.
        assert reverse("challenges:cancel", args=[challenge.pk]) in content
        assert reverse("challenges:remove", args=[challenge.pk, member.pk]) in (content)
        assert (
            reverse("challenges:transfer", args=[challenge.pk, member.user_id])
            in content
        )

        # Creator-only controls (no staff override) are hidden from staff.
        assert reverse("challenges:close", args=[challenge.pk]) not in content
        assert reverse(
            "challenges:regenerate-invite-link", args=[challenge.pk]
        ) not in (content)

    def test_creator_sees_all_controls(self, challenge, mock_sync):
        """The creator still sees every control, including the creator-only ones."""
        member = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)

        content = c.get(
            reverse("challenges:settings", args=[challenge.pk])
        ).content.decode()

        assert reverse("challenges:close", args=[challenge.pk]) in content
        assert reverse("challenges:regenerate-invite-link", args=[challenge.pk]) in (
            content
        )
        assert reverse("challenges:cancel", args=[challenge.pk]) in content
        assert reverse("challenges:remove", args=[challenge.pk, member.pk]) in (content)

    def test_unknown_challenge_404(self, creator_client):
        import uuid

        url = reverse("challenges:settings", args=[uuid.uuid4()])
        response = creator_client.get(url)
        assert response.status_code == 404

    def test_unauthenticated_redirects_to_login(self, challenge):
        url = reverse("challenges:settings", args=[challenge.pk])
        response = Client().get(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


class TestChallengeSettingsViewContent:
    def test_active_challenge_shows_all_actions(
        self, creator_client, challenge, mock_sync
    ):
        """An active challenge offers the creator every management action.

        Asserts the actions and the invite-link endpoint, which are gated on
        status and creator-ness (see the locked/non-creator tests below), not
        the section headings around them — those are copy, and the version of
        this test that listed them only broke on rewordings.
        """
        url = reverse("challenges:settings", args=[challenge.pk])
        response = creator_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        assert challenge.name in content
        # The invite link is the only way anyone joins (TASK-272).
        assert reverse("challenges:regenerate-invite-link", args=[challenge.pk]) in (
            content
        )
        assert reverse("challenges:close", args=[challenge.pk]) in content
        assert reverse("challenges:cancel", args=[challenge.pk]) in content

    def test_ended_but_still_active_challenge_hides_invite_link_actions(
        self, db, mock_sync
    ):
        """end_date has passed but status hasn't flipped to COMPLETED yet (the
        scheduler runs on a ~30-minute cadence) -- is_locked alone misses this
        window, so the invite-link generate/regenerate control must not
        render even though the rest of the Settings page still does."""
        challenge = ChallengeFactory(
            status=Challenge.Status.ACTIVE,
            end_date=(timezone.now() - timedelta(days=1)).date(),
        )
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:settings", args=[challenge.pk])

        content = c.get(url).content.decode()

        assert reverse("challenges:close", args=[challenge.pk]) in content
        assert (
            reverse("challenges:regenerate-invite-link", args=[challenge.pk])
            not in content
        )

    def test_locked_challenge_hides_info_cards(self, creator_client, mock_sync):
        """Terminal challenges suppress the three editable cards, keep Participants.

        Pins the guard split introduced in TASK-280: Participants sits between
        the card grid and Management Actions, both of which are creator-editable
        and must stay hidden once the challenge is terminal.
        """
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        c = Client()
        c.force_login(challenge.creator)

        url = reverse("challenges:settings", args=[challenge.pk])
        response = c.get(url)
        content = response.content.decode()

        assert reverse("challenges:rename", args=[challenge.pk]) not in content
        assert reverse("challenges:history-window", args=[challenge.pk]) not in content
        assert (
            reverse("challenges:regenerate-invite-link", args=[challenge.pk])
            not in content
        )
        assert any(
            t.name == "challenges/_participants_section.html"
            for t in response.templates
        )

    def test_locked_challenge_hides_action_buttons(self, creator_client, mock_sync):
        """Completed/cancelled challenges show participants but no action buttons."""
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:settings", args=[challenge.pk])

        response = c.get(url)

        assert response.status_code == 200
        content = response.content.decode()
        # Participants section still renders
        assert any(
            t.name == "challenges/_participants_section.html"
            for t in response.templates
        )
        # But action buttons don't
        assert reverse("challenges:close", args=[challenge.pk]) not in content
        assert reverse("challenges:cancel", args=[challenge.pk]) not in content

    def test_draft_challenge_shows_actions(self, creator_client, mock_sync):
        """Draft challenges show the management actions section."""
        challenge = ChallengeFactory(status=Challenge.Status.DRAFT)
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:settings", args=[challenge.pk])

        response = c.get(url)

        assert response.status_code == 200
        assert reverse("challenges:close", args=[challenge.pk]).encode() in (
            response.content
        )


class TestParticipantRowsExcludeBailed:
    """TASK-199: voluntarily-left and creator-removed members are erased from
    the settings participant list once they leave."""

    def _rows(self, creator_client, challenge):
        url = reverse("challenges:settings", args=[challenge.pk])
        return creator_client.get(url).context["participant_rows"]

    def test_active_participant_shown_with_action_flags(
        self, creator_client, challenge, mock_sync
    ):
        member = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        )
        rows = self._rows(creator_client, challenge)
        row = next(r for r in rows if r["pk"] == member.pk)
        assert row["can_remove"] is True

    def test_voluntarily_bailed_participant_erased(
        self, creator_client, challenge, mock_sync
    ):
        bailed = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
            is_bailed=True,
            removed_by_creator=False,
        )
        rows = self._rows(creator_client, challenge)
        assert bailed.pk not in {r["pk"] for r in rows}

    def test_creator_removed_participant_erased(
        self, creator_client, challenge, mock_sync
    ):
        removed = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
            is_bailed=True,
            removed_by_creator=True,
        )
        rows = self._rows(creator_client, challenge)
        assert removed.pk not in {r["pk"] for r in rows}


def _with_goal(**kwargs):
    """A ChallengeParticipant with a CustomGoal attached, so the detail view's
    has_goal_configured guard doesn't redirect it to the goal-setup wizard."""
    participant = ChallengeParticipantFactory(**kwargs)
    goal = CustomGoalFactory(participant=participant)
    participant.custom_goal = goal
    participant.save(update_fields=["custom_goal"])
    return participant


class TestDetailPageHasManageLink:
    def test_creator_sees_manage_link_on_detail(
        self, creator_client, challenge, mock_sync
    ):
        """Creator sees 'Manage Challenge' link on detail page."""
        # Creator must be an accepted participant to view detail
        _with_goal(
            challenge=challenge,
            user=challenge.creator,
            invite_status=InviteStatus.ACCEPTED,
        )
        url = reverse("challenges:detail", args=[challenge.pk])
        response = creator_client.get(url)
        assert response.status_code == 200
        settings_url = reverse("challenges:settings", args=[challenge.pk]).encode()
        assert settings_url in response.content

    def test_non_creator_does_not_see_manage_link(self, challenge, mock_sync):
        """Non-creator does not see 'Manage Challenge' link on detail page."""
        other = UserFactory()
        _with_goal(
            challenge=challenge,
            user=other,
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(other)
        url = reverse("challenges:detail", args=[challenge.pk])

        response = c.get(url)

        assert response.status_code == 200
        settings_url = reverse("challenges:settings", args=[challenge.pk]).encode()
        assert settings_url not in response.content

    def test_leave_link_still_on_detail(self, challenge, mock_sync):
        """Verify the leave-challenge link remains on detail for all participants."""
        other = UserFactory()
        _with_goal(
            challenge=challenge,
            user=other,
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(other)
        url = reverse("challenges:detail", args=[challenge.pk])

        response = c.get(url)

        assert response.status_code == 200
        assert reverse("challenges:bail", args=[challenge.pk]).encode() in (
            response.content
        )

    def test_creator_sees_manage_link_on_completed_challenge(self, db, mock_sync):
        """Manage link shows for a completed challenge (settings still useful)."""
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=challenge.creator,
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:detail", args=[challenge.pk])

        response = c.get(url)

        assert response.status_code == 200
        settings_url = reverse("challenges:settings", args=[challenge.pk])
        assert settings_url.encode() in response.content
        # And the settings page loads (read-only) for the completed challenge.
        assert c.get(settings_url).status_code == 200

    def test_staff_sees_manage_link_on_detail(self, challenge, mock_sync):
        """A staff participant sees the Manage link as a settings entry point."""
        staff = UserFactory(is_staff=True)
        _with_goal(
            challenge=challenge,
            user=staff,
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(staff)
        url = reverse("challenges:detail", args=[challenge.pk])

        response = c.get(url)

        assert response.status_code == 200
        settings_url = reverse("challenges:settings", args=[challenge.pk])
        assert settings_url.encode() in response.content


class TestParticipantsSectionVisibility:
    """Salvaged from the deleted test_invite_participants.py (TASK-272): these
    cover the participants section generally, not the removed invite lifecycle."""

    @pytest.fixture
    def configured_creator_challenge(self, db):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=challenge.creator,
            invite_status=InviteStatus.ACCEPTED,
        )
        return challenge

    def test_creator_sees_participants_section(
        self, configured_creator_challenge, mock_sync
    ):
        challenge = configured_creator_challenge
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:settings", args=[challenge.pk])

        response = c.get(url)

        assert response.status_code == 200
        assert any(
            t.name == "challenges/_participants_section.html"
            for t in response.templates
        )

    def test_deactivated_participant_shown_with_deleted_suffix(
        self, configured_creator_challenge, mock_sync
    ):
        challenge = configured_creator_challenge
        gone = UserFactory(display_name="Gone User", is_active=False)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=gone,
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:settings", args=[challenge.pk])

        response = c.get(url)

        content = response.content.decode()
        assert "Gone User (deleted)" in content

    def test_non_creator_does_not_see_participants_section(
        self, configured_creator_challenge, mock_sync
    ):
        challenge = configured_creator_challenge
        other = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=other,
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(other)
        url = reverse("challenges:settings", args=[challenge.pk])

        response = c.get(url)

        assert response.status_code == 403
