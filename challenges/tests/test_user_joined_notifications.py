"""Tests for user_joined notification triggers (TASK-33).

Every join now goes through the invite link (TASK-272 removed the accept/decline
and direct-join paths), so all of these drive ``challenges:invite-link``.
"""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeInviteLinkFactory,
    ChallengeParticipantFactory,
)
from notifications.models import Notification


def _accepted(challenge, user):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )


def _join_via_link(challenge, user):
    link = ChallengeInviteLinkFactory(challenge=challenge, created_by=challenge.creator)
    client = Client()
    client.force_login(user)
    return client.get(reverse("challenges:invite-link", args=[link.token]))


@pytest.mark.django_db
class TestJoinViaLinkNotifications:
    def test_notifies_all_existing_accepted_participants(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        existing_a = UserFactory()
        existing_b = UserFactory()
        _accepted(challenge, existing_a)
        _accepted(challenge, existing_b)

        joiner = UserFactory(liftosaur_api_key="test-liftosaur-key")
        _join_via_link(challenge, joiner)

        notes = Notification.objects.filter(
            event_type=Notification.EventType.USER_JOINED
        )
        assert notes.count() == 2
        assert set(notes.values_list("user", flat=True)) == {
            existing_a.pk,
            existing_b.pk,
        }
        assert all(n.challenge_id == challenge.pk for n in notes)
        assert all(n.metadata["joined_user_name"] == str(joiner) for n in notes)

    def test_does_not_notify_the_joining_user(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(challenge, UserFactory())

        joiner = UserFactory(liftosaur_api_key="test-liftosaur-key")
        _join_via_link(challenge, joiner)

        assert (
            Notification.objects.filter(
                event_type=Notification.EventType.USER_JOINED, user=joiner
            ).count()
            == 0
        )

    def test_excludes_legacy_invited_and_bailed_participants(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        accepted = UserFactory()
        invited = UserFactory()
        bailed = UserFactory()
        _accepted(challenge, accepted)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=invited,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        ChallengeParticipantFactory(
            challenge=challenge,
            user=bailed,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )

        joiner = UserFactory(liftosaur_api_key="test-liftosaur-key")
        _join_via_link(challenge, joiner)

        notes = Notification.objects.filter(
            event_type=Notification.EventType.USER_JOINED
        )
        assert set(notes.values_list("user", flat=True)) == {accepted.pk}

    def test_rejoin_after_bail_notifies_existing_participants(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        existing = UserFactory()
        _accepted(challenge, existing)

        rejoiner = UserFactory(liftosaur_api_key="test-liftosaur-key")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=rejoiner,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )

        _join_via_link(challenge, rejoiner)

        notes = Notification.objects.filter(
            event_type=Notification.EventType.USER_JOINED
        )
        assert notes.count() == 1
        note = notes.get()
        assert note.user == existing
        assert note.metadata["joined_user_name"] == str(rejoiner)
