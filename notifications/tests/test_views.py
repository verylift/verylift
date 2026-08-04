"""Tests for the notifications feed views (TASK-32, reworked in TASK-246).

The standalone notifications page was removed in TASK-246; the feed renders as
a section of the dashboard, so list-shaped assertions go through
``challenges:dashboard``.
"""

import pytest
from django.test import Client
from django.urls import NoReverseMatch, reverse

from accounts.tests.factories import UserFactory
from challenges.models import ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)
from notifications.models import Notification
from notifications.tests.factories import NotificationFactory


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def authed_client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestNotificationsList:
    def test_login_required(self):
        response = Client().get(reverse("challenges:dashboard"))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_standalone_page_removed(self):
        with pytest.raises(NoReverseMatch):
            reverse("notifications:list")

    def test_only_requesting_users_notifications_shown(self, authed_client, user):
        mine = NotificationFactory(user=user)
        theirs = NotificationFactory(user=UserFactory())
        response = authed_client.get(reverse("challenges:dashboard"))
        rows = response.context["rows"]
        ids = {row["notification"].pk for row in rows}
        assert mine.pk in ids
        assert theirs.pk not in ids

    def test_ordered_by_created_at_descending(self, authed_client, user):
        first = NotificationFactory(user=user)
        second = NotificationFactory(user=user)
        response = authed_client.get(reverse("challenges:dashboard"))
        rows = response.context["rows"]
        assert [r["notification"].pk for r in rows] == [second.pk, first.pk]

    def test_section_caps_at_30_newest_first(self, authed_client, user):
        notifications = NotificationFactory.create_batch(35, user=user)
        response = authed_client.get(reverse("challenges:dashboard"))
        rows = response.context["rows"]
        assert len(rows) == 30
        assert rows[0]["notification"].pk == notifications[-1].pk

    def test_hides_read_notifications_by_default(self, authed_client, user):
        unread = NotificationFactory(user=user, is_read=False)
        NotificationFactory(user=user, is_read=True)
        response = authed_client.get(reverse("challenges:dashboard"))
        rows = response.context["rows"]
        assert [r["notification"].pk for r in rows] == [unread.pk]
        assert response.context["show_read"] is False

    def test_caught_up_state_when_only_read_notifications_exist(
        self, authed_client, user
    ):
        NotificationFactory(user=user, is_read=True)
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"] == []
        assert response.context["has_notifications"] is True
        assert "You're all caught up." in response.content.decode()
        assert "You have no notifications." not in response.content.decode()

    def test_true_empty_state_when_no_notifications_at_all(self, authed_client, user):
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"] == []
        assert response.context["has_notifications"] is False
        assert "You have no notifications." in response.content.decode()

    def test_dashboard_honors_show_read_query_param(self, authed_client, user):
        read = NotificationFactory(user=user, is_read=True)
        response = authed_client.get(reverse("challenges:dashboard") + "?show_read=1")
        rows = response.context["rows"]
        assert response.context["show_read"] is True
        assert read.pk in {r["notification"].pk for r in rows}


@pytest.mark.django_db
class TestReadVisibilityToggle:
    def test_login_required(self):
        response = Client().get(reverse("notifications:section"))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_htmx_show_read_reveals_read_notifications(self, authed_client, user):
        read = NotificationFactory(user=user, is_read=True)
        response = authed_client.get(
            reverse("notifications:section") + "?show_read=1",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        rows = response.context["rows"]
        assert read.pk in {r["notification"].pk for r in rows}
        assert response.context["show_read"] is True

    def test_htmx_default_hides_read_notifications(self, authed_client, user):
        NotificationFactory(user=user, is_read=True)
        unread = NotificationFactory(user=user, is_read=False)
        response = authed_client.get(
            reverse("notifications:section"),
            HTTP_HX_REQUEST="true",
        )
        rows = response.context["rows"]
        assert [r["notification"].pk for r in rows] == [unread.pk]

    def test_plain_request_redirects_to_dashboard_carrying_state(
        self, authed_client, user
    ):
        response = authed_client.get(reverse("notifications:section") + "?show_read=1")
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard") + "?show_read=1"

    def test_plain_request_redirects_without_query_string_by_default(
        self, authed_client, user
    ):
        response = authed_client.get(reverse("notifications:section"))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")


@pytest.mark.django_db
class TestDisplayText:
    def test_invite_received_text(self, authed_client, user):
        comp = ChallengeFactory(name="Summer Slam")
        NotificationFactory(
            user=user,
            event_type=Notification.EventType.INVITE_RECEIVED,
            challenge=comp,
        )
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"][0]["text"] == (
            "You have been invited to Summer Slam"
        )

    def test_user_joined_text_from_metadata(self, authed_client, user):
        comp = ChallengeFactory(name="Summer Slam")
        NotificationFactory(
            user=user,
            event_type=Notification.EventType.USER_JOINED,
            challenge=comp,
            metadata={"joined_user_name": "Alice"},
        )
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"][0]["text"] == "Alice joined Summer Slam"

    def test_overtaken_text_from_metadata(self, authed_client, user):
        comp = ChallengeFactory(name="Summer Slam")
        NotificationFactory(
            user=user,
            event_type=Notification.EventType.OVERTAKEN,
            challenge=comp,
            metadata={"overtaken_by_name": "Bob"},
        )
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"][0]["text"] == "Bob passed you in Summer Slam"

    def test_challenge_closed_text(self, authed_client, user):
        comp = ChallengeFactory(name="Summer Slam")
        NotificationFactory(
            user=user,
            event_type=Notification.EventType.CHALLENGE_CLOSED,
            challenge=comp,
        )
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"][0]["text"] == (
            "Summer Slam has ended — view your final placing"
        )

    def test_removed_from_challenge_text(self, authed_client, user):
        comp = ChallengeFactory(name="Summer Slam")
        NotificationFactory(
            user=user,
            event_type=Notification.EventType.REMOVED_FROM_CHALLENGE,
            challenge=comp,
        )
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"][0]["text"] == (
            "You have been removed from Summer Slam"
        )

    def test_text_falls_back_when_challenge_missing(self, authed_client, user):
        NotificationFactory(
            user=user,
            event_type=Notification.EventType.INVITE_RECEIVED,
            challenge=None,
        )
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"][0]["text"] == (
            "You have been invited to a challenge"
        )

    def test_user_joined_falls_back_without_metadata(self, authed_client, user):
        comp = ChallengeFactory(name="Summer Slam")
        NotificationFactory(
            user=user,
            event_type=Notification.EventType.USER_JOINED,
            challenge=comp,
            metadata={},
        )
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"][0]["text"] == "Someone joined Summer Slam"

    def test_overtaken_falls_back_without_metadata(self, authed_client, user):
        comp = ChallengeFactory(name="Summer Slam")
        NotificationFactory(
            user=user,
            event_type=Notification.EventType.OVERTAKEN,
            challenge=comp,
            metadata={},
        )
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["rows"][0]["text"] == (
            "Someone passed you in Summer Slam"
        )


@pytest.mark.django_db
class TestMarkRead:
    def test_post_marks_read(self, authed_client, user):
        comp = ChallengeFactory()
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.CHALLENGE_CLOSED,
            challenge=comp,
            is_read=False,
        )
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        n.refresh_from_db()
        assert n.is_read is True
        assert response.status_code == 302

    def test_legacy_pending_invite_redirects_to_dashboard(self, authed_client, user):
        """TASK-272: there is no dashboard invite card left to anchor to, and
        detail would 403 for a non-accepted row — so it lands on the dashboard
        itself."""
        comp = ChallengeFactory()
        ChallengeParticipantFactory(
            challenge=comp,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.INVITE_RECEIVED,
            challenge=comp,
        )
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        assert response["Location"] == reverse("challenges:dashboard")

    def test_already_accepted_invite_redirects_to_detail(self, authed_client, user):
        comp = ChallengeFactory()
        ChallengeParticipantFactory(
            challenge=comp,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.INVITE_RECEIVED,
            challenge=comp,
        )
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:detail", args=[comp.pk])

    def test_already_declined_invite_redirects_to_dashboard(self, authed_client, user):
        comp = ChallengeFactory()
        ChallengeParticipantFactory(
            challenge=comp,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.DECLINED,
        )
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.INVITE_RECEIVED,
            challenge=comp,
        )
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")

    def test_invite_without_participant_redirects_to_dashboard(
        self, authed_client, user
    ):
        comp = ChallengeFactory()
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.INVITE_RECEIVED,
            challenge=comp,
        )
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        assert response["Location"] == reverse("challenges:dashboard")

    def test_non_invite_redirects_to_detail(self, authed_client, user):
        comp = ChallengeFactory()
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.OVERTAKEN,
            challenge=comp,
        )
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        assert response["Location"] == reverse("challenges:detail", args=[comp.pk])

    def test_removed_from_challenge_redirects_to_dashboard(self, authed_client, user):
        comp = ChallengeFactory()
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.REMOVED_FROM_CHALLENGE,
            challenge=comp,
        )
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        assert response["Location"] == reverse("challenges:dashboard")

    def test_redirects_to_dashboard_when_challenge_missing(self, authed_client, user):
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.OVERTAKEN,
            challenge=None,
        )
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        assert response["Location"] == reverse("challenges:dashboard")

    def test_cannot_mark_another_users_notification(self, authed_client):
        n = NotificationFactory(user=UserFactory())
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        assert response.status_code == 404
        n.refresh_from_db()
        assert n.is_read is False

    def test_get_not_allowed(self, authed_client, user):
        n = NotificationFactory(user=user)
        response = authed_client.get(reverse("notifications:read", args=[n.pk]))
        assert response.status_code == 405


@pytest.mark.django_db
class TestMarkAllRead:
    def test_marks_all_unread_for_user(self, authed_client, user):
        unread = NotificationFactory.create_batch(3, user=user, is_read=False)
        other_user_unread = NotificationFactory(user=UserFactory(), is_read=False)
        response = authed_client.post(reverse("notifications:read-all"))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
        for n in unread:
            n.refresh_from_db()
            assert n.is_read is True
        other_user_unread.refresh_from_db()
        assert other_user_unread.is_read is False

    def test_get_not_allowed(self, authed_client):
        response = authed_client.get(reverse("notifications:read-all"))
        assert response.status_code == 405

    def test_plain_request_preserves_show_read_in_redirect(self, authed_client, user):
        NotificationFactory(user=user, is_read=False)
        response = authed_client.post(
            reverse("notifications:read-all") + "?show_read=1"
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard") + "?show_read=1"


@pytest.mark.django_db
class TestUnreadCountContext:
    def test_count_in_context_accurate(self, authed_client, user):
        NotificationFactory.create_batch(2, user=user, is_read=False)
        NotificationFactory(user=user, is_read=True)
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["unread_notification_count"] == 2

    def test_count_zero_when_unauthenticated(self):
        from notifications.context_processors import unread_notification_count

        class AnonRequest:
            class user:
                is_authenticated = False

        assert unread_notification_count(AnonRequest()) == {
            "unread_notification_count": 0
        }

    def test_count_excludes_other_users(self, authed_client, user):
        NotificationFactory(user=user, is_read=False)
        NotificationFactory(user=UserFactory(), is_read=False)
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.context["unread_notification_count"] == 1


@pytest.mark.django_db
class TestMarkReadHTMX:
    def test_htmx_marks_read_and_hx_redirects(self, authed_client, user):
        comp = ChallengeFactory()
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.CHALLENGE_CLOSED,
            challenge=comp,
            is_read=False,
        )
        response = authed_client.post(
            reverse("notifications:read", args=[n.pk]),
            HTTP_HX_REQUEST="true",
        )
        n.refresh_from_db()
        assert n.is_read is True
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("challenges:detail", args=[comp.pk])

    def test_htmx_legacy_pending_invite_navigates_to_dashboard(
        self, authed_client, user
    ):
        comp = ChallengeFactory()
        ChallengeParticipantFactory(
            challenge=comp,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.INVITE_RECEIVED,
            challenge=comp,
            is_read=False,
        )
        response = authed_client.post(
            reverse("notifications:read", args=[n.pk]),
            HTTP_HX_REQUEST="true",
        )
        n.refresh_from_db()
        assert n.is_read is True
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("challenges:dashboard")

    def test_htmx_accepted_invite_navigates_to_detail(self, authed_client, user):
        comp = ChallengeFactory()
        ChallengeParticipantFactory(
            challenge=comp,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.INVITE_RECEIVED,
            challenge=comp,
            is_read=False,
        )
        response = authed_client.post(
            reverse("notifications:read", args=[n.pk]),
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("challenges:detail", args=[comp.pk])

    def test_htmx_non_invite_navigates_to_detail(self, authed_client, user):
        comp = ChallengeFactory()
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.OVERTAKEN,
            challenge=comp,
            is_read=False,
        )
        response = authed_client.post(
            reverse("notifications:read", args=[n.pk]),
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("challenges:detail", args=[comp.pk])

    def test_htmx_removed_navigates_to_dashboard(self, authed_client, user):
        comp = ChallengeFactory()
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.REMOVED_FROM_CHALLENGE,
            challenge=comp,
            is_read=False,
        )
        response = authed_client.post(
            reverse("notifications:read", args=[n.pk]),
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("challenges:dashboard")

    def test_htmx_navigates_to_dashboard_when_challenge_missing(
        self, authed_client, user
    ):
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.OVERTAKEN,
            challenge=None,
            is_read=False,
        )
        response = authed_client.post(
            reverse("notifications:read", args=[n.pk]),
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("challenges:dashboard")

    def test_plain_request_still_redirects(self, authed_client, user):
        comp = ChallengeFactory()
        n = NotificationFactory(
            user=user,
            event_type=Notification.EventType.CHALLENGE_CLOSED,
            challenge=comp,
            is_read=False,
        )
        response = authed_client.post(reverse("notifications:read", args=[n.pk]))
        assert response.status_code == 302


@pytest.mark.django_db
class TestMarkAllReadHTMX:
    def test_htmx_returns_caught_up_state_by_default(self, authed_client, user):
        """Default view is unread-only, so marking everything read empties
        the section down to the caught-up state rather than still listing
        the now-read rows."""
        NotificationFactory.create_batch(3, user=user, is_read=False)
        response = authed_client.post(
            reverse("notifications:read-all"),
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="notifications-list"' not in content
        assert "You're all caught up." in content
        # All notifications should now be marked read
        assert Notification.objects.filter(user=user, is_read=False).count() == 0

    def test_htmx_preserves_show_read_state(self, authed_client, user):
        """Marking all as read from the 'show read' view keeps showing the
        (now all-read) rows rather than switching the user back to
        unread-only underneath them."""
        NotificationFactory.create_batch(3, user=user, is_read=False)
        response = authed_client.post(
            reverse("notifications:read-all") + "?show_read=1",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="notifications-list"' in content
        assert Notification.objects.filter(user=user, is_read=False).count() == 0

    def test_htmx_includes_oob_messages(self, authed_client, user):
        NotificationFactory(user=user, is_read=False)
        response = authed_client.post(
            reverse("notifications:read-all"),
            HTTP_HX_REQUEST="true",
        )
        # Check that OOB messages div is present
        assert 'id="app-messages"' in response.content.decode()
        assert 'hx-swap-oob="innerHTML"' in response.content.decode()

    def test_plain_request_still_redirects(self, authed_client, user):
        NotificationFactory(user=user, is_read=False)
        response = authed_client.post(reverse("notifications:read-all"))
        assert response.status_code == 302
