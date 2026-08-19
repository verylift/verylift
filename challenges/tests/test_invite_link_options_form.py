"""Tests for InviteLinkOptionsForm's "Never expires" checkbox (issue #33)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from challenges.forms import InviteLinkOptionsForm
from challenges.tests.factories import ChallengeFactory


@pytest.mark.django_db
class TestInviteLinkOptionsFormNeverExpires:
    def test_never_expires_checked_forces_expires_at_none(self):
        challenge = ChallengeFactory(
            end_date=(timezone.now() + timedelta(days=30)).date()
        )
        form = InviteLinkOptionsForm(
            data={"never_expires": "on", "max_uses": ""}, challenge=challenge
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["expires_at"] is None

    def test_never_expires_checked_wins_over_a_submitted_date(self):
        challenge = ChallengeFactory(
            end_date=(timezone.now() + timedelta(days=30)).date()
        )
        future = timezone.now() + timedelta(days=2)
        form = InviteLinkOptionsForm(
            data={
                "never_expires": "on",
                "expires_at": future.strftime("%Y-%m-%dT%H:%M"),
                "max_uses": "",
            },
            challenge=challenge,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["expires_at"] is None

    def test_never_expires_checked_ignores_an_invalid_date(self):
        """A past date would normally fail clean_expires_at -- the checkbox
        must suppress that error entirely, not just override the value."""
        challenge = ChallengeFactory(
            end_date=(timezone.now() + timedelta(days=30)).date()
        )
        past = timezone.now() - timedelta(days=1)
        form = InviteLinkOptionsForm(
            data={
                "never_expires": "on",
                "expires_at": past.strftime("%Y-%m-%dT%H:%M"),
                "max_uses": "",
            },
            challenge=challenge,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["expires_at"] is None

    def test_never_expires_unchecked_keeps_existing_expiry_validation(self):
        challenge = ChallengeFactory(
            end_date=(timezone.now() + timedelta(days=30)).date()
        )
        past = timezone.now() - timedelta(days=1)
        form = InviteLinkOptionsForm(
            data={"expires_at": past.strftime("%Y-%m-%dT%H:%M"), "max_uses": ""},
            challenge=challenge,
        )
        assert not form.is_valid()
        assert "expires_at" in form.errors

    def test_never_expires_unchecked_and_blank_date_returns_none(self):
        """Blank date + unchecked box means "use the challenge default" --
        the view/service layer applies that default, not the form."""
        challenge = ChallengeFactory()
        form = InviteLinkOptionsForm(
            data={"expires_at": "", "max_uses": ""}, challenge=challenge
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["expires_at"] is None
        assert form.cleaned_data["never_expires"] is False
