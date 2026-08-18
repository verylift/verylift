"""Tests for the bail (leave challenge) view (TASK-18)."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant, CustomGoal
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
    CustomGoalFactory,
)


@pytest.fixture
def member(db):
    return UserFactory()


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE)


@pytest.fixture
def participant(challenge, member):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=member,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )


@pytest.fixture
def member_client(member):
    c = Client()
    c.force_login(member)
    return c


class TestBail:
    def test_post_sets_is_bailed_and_bailed_at(
        self, member_client, participant, challenge
    ):
        url = reverse("challenges:bail", args=[challenge.pk])
        response = member_client.post(url)
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
        participant.refresh_from_db()
        assert participant.is_bailed is True
        assert participant.bailed_at is not None

    def test_bailing_detaches_the_active_goal_without_deleting_it(
        self, member_client, participant, challenge
    ):
        """Rejoining via invite link un-bails this same participant row
        rather than creating a fresh one -- if custom_goal weren't cleared
        here, has_goal_configured would stay True and silently resurrect
        the old goal instead of prompting a new one."""
        goal = CustomGoalFactory(participant=participant, name="My Goal")
        participant.custom_goal = goal
        participant.save(update_fields=["custom_goal"])

        url = reverse("challenges:bail", args=[challenge.pk])
        member_client.post(url)

        participant.refresh_from_db()
        goal.refresh_from_db()
        assert participant.custom_goal_id is None
        assert participant.has_goal_configured is False
        assert CustomGoal.objects.filter(pk=goal.pk).exists()
        assert goal.name != "My Goal"

    def test_rejoining_can_reuse_the_same_goal_name_after_bailing(
        self, member_client, participant, challenge
    ):
        """Regression: CustomGoal has a unique constraint on (participant,
        name), and the same participant row persists across a leave/rejoin
        cycle -- without renaming the detached goal, picking the same name
        again (e.g. the "My Goal" default) hit an IntegrityError against the
        participant's own archived goal."""
        goal = CustomGoalFactory(participant=participant, name="My Goal")
        participant.custom_goal = goal
        participant.save(update_fields=["custom_goal"])

        url = reverse("challenges:bail", args=[challenge.pk])
        member_client.post(url)

        participant.refresh_from_db()
        new_goal = CustomGoalFactory(participant=participant, name="My Goal")
        participant.custom_goal = new_goal
        participant.save(update_fields=["custom_goal"])

        assert CustomGoal.objects.filter(participant=participant).count() == 2

    def test_get_renders_confirmation_page(self, member_client, participant, challenge):
        url = reverse("challenges:bail", args=[challenge.pk])
        response = member_client.get(url)
        assert response.status_code == 200
        assert b"Leave Challenge" in response.content
        assert challenge.name.encode() in response.content
        participant.refresh_from_db()
        assert participant.is_bailed is False

    def test_requires_login(self, db, participant, challenge):
        url = reverse("challenges:bail", args=[challenge.pk])
        response = Client().post(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_non_participant_gets_403(self, db, challenge):
        outsider = UserFactory()
        c = Client()
        c.force_login(outsider)
        url = reverse("challenges:bail", args=[challenge.pk])
        assert c.post(url).status_code == 403
        assert c.get(url).status_code == 403

    def test_already_bailed_gets_400(self, member_client, participant, challenge):
        participant.is_bailed = True
        participant.save()
        url = reverse("challenges:bail", args=[challenge.pk])
        assert member_client.post(url).status_code == 400
        assert member_client.get(url).status_code == 400

    def test_invited_but_not_accepted_gets_400(self, member_client, participant):
        participant.invite_status = ChallengeParticipant.InviteStatus.INVITED
        participant.save()
        url = reverse("challenges:bail", args=[participant.challenge.pk])
        assert member_client.post(url).status_code == 400

    def test_declined_gets_400(self, member_client, participant):
        participant.invite_status = ChallengeParticipant.InviteStatus.DECLINED
        participant.save()
        url = reverse("challenges:bail", args=[participant.challenge.pk])
        assert member_client.post(url).status_code == 400

    def test_bail_on_completed_challenge_gets_400(self, member_client, member):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=member,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        url = reverse("challenges:bail", args=[challenge.pk])
        assert member_client.post(url).status_code == 400
        participant.refresh_from_db()
        assert participant.is_bailed is False
