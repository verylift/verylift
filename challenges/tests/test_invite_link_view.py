"""Tests for the invite-link landing/join view (TASK-249)."""

from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeInviteLinkFactory,
    ChallengeParticipantFactory,
)


def _tiny_png(name="avatar.png"):
    buffer = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


InviteStatus = ChallengeParticipant.InviteStatus


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE)


@pytest.fixture
def link(challenge):
    return ChallengeInviteLinkFactory(
        challenge=challenge,
        revoked_at=None,
        expires_at=timezone.now() + timedelta(days=7),
    )


class TestUnknownToken:
    def test_unknown_token_404s(self, db):
        response = Client().get(reverse("challenges:invite-link", args=["nope"]))
        assert response.status_code == 404


class TestExpiredOrRevokedToken:
    def test_expired_token_renders_invalid_page(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        response = Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert challenge.name.encode() in response.content

    def test_revoked_token_renders_invalid_page(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        response = Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert challenge.name.encode() in response.content

    def test_exhausted_token_renders_invalid_page(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
            max_uses=1,
            use_count=1,
        )
        response = Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert challenge.name.encode() in response.content

    def test_expired_token_works_for_an_authenticated_visitor_too(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        c = Client()
        c.force_login(UserFactory())
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert challenge.name.encode() in response.content


class TestAnonymousVisitor:
    def test_stashes_token_in_session_and_renders_og_tagged_welcome_page(
        self, challenge, link
    ):
        c = Client()
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert c.session["invite_token"] == link.token
        assert challenge.name.encode() in response.content
        assert str(link.created_by).encode() in response.content

    def test_og_title_contains_the_challenge_name(self, challenge, link):
        response = Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert (
            f'<meta property="og:title" content="Join {challenge.name} on very lift">'
        ).encode() in response.content

    def test_welcome_page_links_to_register_login_and_landing(self, challenge, link):
        response = Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert reverse("accounts:register").encode() in response.content
        assert reverse("accounts:login").encode() in response.content
        assert reverse("core:landing").encode() in response.content

    def test_does_not_auto_create_an_account(self, challenge, link):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        before = User.objects.count()
        Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert User.objects.count() == before

    def test_inviter_photo_falls_back_to_initials_for_an_anonymous_visitor(
        self, challenge, link
    ):
        """Uploaded photos are served from /media/, which is gated behind
        request.user.is_authenticated with no per-resource exception -- an
        anonymous visitor can't fetch the real image, so this page must not
        try to render it (it would just be a broken <img>)."""
        link.created_by.avatar = _tiny_png()
        link.created_by.save()

        response = Client().get(reverse("challenges:invite-link", args=[link.token]))

        assert f'src="{link.created_by.avatar.url}"'.encode() not in response.content
        assert str(link.created_by).encode() in response.content


class TestLinkPreviewCrawlerAndPrivacy:
    """TASK-38 remainder: the anonymous preview must be reachable by link-
    preview crawlers (no Django-level UA blocking) and must never leak
    another participant's identity or performance data."""

    def test_facebook_crawler_user_agent_gets_200(self, challenge, link):
        response = Client().get(
            reverse("challenges:invite-link", args=[link.token]),
            HTTP_USER_AGENT="facebookexternalhit/1.1 Facebook",
        )
        assert response.status_code == 200

    def test_anonymous_visit_is_200_with_no_redirect(self, challenge, link):
        response = Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200

    def test_no_participant_identity_or_lift_data_leaks(self, challenge, link):
        from liftosaur.tests.factories import LiftHistoryFactory

        secret_lift_weight = "313.37"
        participants = [
            ChallengeParticipantFactory(
                challenge=challenge,
                invite_status=InviteStatus.ACCEPTED,
                joined_at=datetime.now(tz=UTC),
            )
            for _ in range(2)
        ]
        LiftHistoryFactory(
            user=participants[0].user,
            weight_kg=secret_lift_weight,
        )

        response = Client().get(reverse("challenges:invite-link", args=[link.token]))
        body = response.content

        for participant in participants:
            assert participant.user.username.encode() not in body
            assert str(participant.user).encode() not in body
        assert secret_lift_weight.encode() not in body


@pytest.mark.django_db
class TestAuthenticatedFreshVisitor:
    """TASK-303: a fresh (never-a-participant) authenticated visitor now sees
    the accept/decline preview instead of being auto-joined."""

    def test_renders_accept_page_without_joining(self, challenge, link):
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))

        assert response.status_code == 200
        assert not ChallengeParticipant.objects.filter(
            challenge=challenge, user=user
        ).exists()

    def test_does_not_increment_the_links_use_count(self, challenge, link):
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)
        c.get(reverse("challenges:invite-link", args=[link.token]))
        link.refresh_from_db()
        assert link.use_count == 0

    def test_draft_challenge_renders_accept_page(self, link):
        challenge = link.challenge
        challenge.status = Challenge.Status.DRAFT
        challenge.save(update_fields=["status"])
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert not ChallengeParticipant.objects.filter(
            challenge=challenge, user=user
        ).exists()

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_terminal_challenge_is_not_joinable(self, link, status):
        # Used to be a raw 400; now the dedicated "start your own" page
        # (TestTerminalChallenge covers that page's content in full) --
        # this just confirms joining itself still never happens.
        challenge = link.challenge
        challenge.status = status
        challenge.save(update_fields=["status"])
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert not ChallengeParticipant.objects.filter(
            challenge=challenge, user=user
        ).exists()


@pytest.mark.django_db
class TestIdempotentRevisit:
    def test_existing_accepted_participant_redirected_to_detail(self, challenge, link):
        user = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=datetime.now(tz=UTC),
        )
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:detail", args=[challenge.pk])

    def test_revisit_does_not_increment_use_count(self, challenge, link):
        user = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=datetime.now(tz=UTC),
        )
        c = Client()
        c.force_login(user)
        c.get(reverse("challenges:invite-link", args=[link.token]))
        link.refresh_from_db()
        assert link.use_count == 0


class TestTerminalChallenge:
    """A never-expiring link (issue #33) can stay live long after its
    challenge ends -- both visitor types must land on the "start your own"
    page rather than either a dead-end join-preview (anonymous) or the raw
    400 this used to return (authenticated)."""

    @pytest.fixture
    def ended_challenge(self, db):
        return ChallengeFactory(status=Challenge.Status.COMPLETED)

    @pytest.fixture
    def never_expiring_link(self, ended_challenge):
        return ChallengeInviteLinkFactory(
            challenge=ended_challenge, revoked_at=None, expires_at=None
        )

    def test_anonymous_visitor_sees_ended_page_not_the_join_preview(
        self, ended_challenge, never_expiring_link
    ):
        c = Client()
        response = c.get(
            reverse("challenges:invite-link", args=[never_expiring_link.token])
        )
        assert response.status_code == 200
        assert ended_challenge.name.encode() in response.content
        assert any(
            t.name == "challenges/invite_link_ended.html" for t in response.templates
        )

    def test_anonymous_visitor_does_not_get_invite_token_stashed(
        self, ended_challenge, never_expiring_link
    ):
        c = Client()
        c.get(reverse("challenges:invite-link", args=[never_expiring_link.token]))
        assert "invite_token" not in c.session

    def test_authenticated_visitor_sees_ended_page_not_a_400(
        self, ended_challenge, never_expiring_link
    ):
        c = Client()
        c.force_login(UserFactory())
        response = c.get(
            reverse("challenges:invite-link", args=[never_expiring_link.token])
        )
        assert response.status_code == 200
        assert any(
            t.name == "challenges/invite_link_ended.html" for t in response.templates
        )

    def test_shows_participant_count(self, ended_challenge, never_expiring_link):
        ChallengeParticipantFactory(
            challenge=ended_challenge,
            invite_status=InviteStatus.ACCEPTED,
        )
        response = Client().get(
            reverse("challenges:invite-link", args=[never_expiring_link.token])
        )
        assert b"1 person competed" in response.content

    def test_bounded_but_unexpired_link_also_shows_ended_page_once_cancelled(self, db):
        # A link's own expires_at can still be in the future while the
        # challenge it points to has already ended some other way (e.g.
        # cancelled by its creator) -- the challenge's own terminal status
        # must win regardless of the link's remaining expiry.
        comp = ChallengeFactory(status=Challenge.Status.CANCELLED)
        bounded_link = ChallengeInviteLinkFactory(
            challenge=comp,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        response = Client().get(
            reverse("challenges:invite-link", args=[bounded_link.token])
        )
        assert any(
            t.name == "challenges/invite_link_ended.html" for t in response.templates
        )


@pytest.mark.django_db
class TestVoluntaryBailRejoin:
    def test_rejoin_resets_participant_and_records_new_link(self, challenge, link):
        user = UserFactory(liftosaur_api_key="test-key")
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=datetime(2025, 1, 1, tzinfo=UTC),
            is_bailed=True,
            bailed_at=datetime(2025, 1, 2, tzinfo=UTC),
            removed_by_creator=False,
        )
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 302

        participant.refresh_from_db()
        assert participant.is_bailed is False
        assert participant.bailed_at is None
        assert participant.invite_status == InviteStatus.ACCEPTED
        assert participant.joined_via_link_id == link.pk

    def test_rejoin_increments_the_links_use_count(self, challenge, link):
        user = UserFactory(liftosaur_api_key="test-key")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=datetime(2025, 1, 1, tzinfo=UTC),
            is_bailed=True,
            bailed_at=datetime(2025, 1, 2, tzinfo=UTC),
            removed_by_creator=False,
        )
        c = Client()
        c.force_login(user)
        c.get(reverse("challenges:invite-link", args=[link.token]))
        link.refresh_from_db()
        assert link.use_count == 1


@pytest.mark.django_db
class TestRemovedByCreatorBlocked:
    def test_removed_participant_cannot_rejoin_via_link(self, challenge, link):
        user = UserFactory(liftosaur_api_key="test-key")
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=datetime(2025, 1, 1, tzinfo=UTC),
            is_bailed=True,
            bailed_at=datetime(2025, 1, 2, tzinfo=UTC),
            removed_by_creator=True,
        )
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 400

        participant.refresh_from_db()
        assert participant.is_bailed is True
        assert participant.removed_by_creator is True


@pytest.mark.django_db
class TestKeylessJoin:
    """Joining a challenge never requires a Liftosaur key -- manual self-report
    and Hevy CSV import are equally valid ways to log lifts, so a lifter with
    zero tracker connected can join exactly like a challenge's creator
    already could (the creator is auto-added at creation, bypassing this view
    entirely)."""

    def test_keyless_visitor_sees_accept_page_without_a_liftosaur_key(
        self, challenge, link
    ):
        user = UserFactory(liftosaur_api_key=None)
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))

        assert response.status_code == 200
        assert not ChallengeParticipant.objects.filter(
            challenge=challenge, user=user
        ).exists()

    def test_keyless_visitor_can_rejoin_after_bailing(self, challenge, link):
        user = UserFactory(liftosaur_api_key=None)
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=datetime(2025, 1, 1, tzinfo=UTC),
            is_bailed=True,
            bailed_at=datetime(2025, 1, 2, tzinfo=UTC),
            removed_by_creator=False,
        )
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))

        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:goal-setup", args=[challenge.pk]
        )
        participant.refresh_from_db()
        assert participant.is_bailed is False
        assert participant.joined_via_link_id == link.pk
