"""Tests for self-serve account deletion / anonymization (#46, TASK-308)."""

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from PIL import Image

from accounts.forms import DeleteAccountConfirmationForm
from accounts.services import _unique_email, _unique_username, anonymize_account
from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_custom_challenge,
)
from notifications.models import Notification
from scoring.tests.factories import PointEarnEventFactory

User = get_user_model()


def _png(name="avatar.png"):
    buffer = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def authed_client(user):
    c = Client()
    c.force_login(user)
    return c


class TestUniqueUsername:
    def test_returns_generated_pseudonym(self, user, db):
        with patch(
            "accounts.services._pseudonym_candidate",
            return_value=("Swift", "Falcon"),
        ):
            result = _unique_username(user)
        assert result == "SwiftFalcon"

    def test_rerolls_on_collision(self, user, db):
        UserFactory(username="SwiftFalcon")
        with patch(
            "accounts.services._pseudonym_candidate",
            side_effect=[("Swift", "Falcon"), ("Quiet", "Otter")],
        ):
            result = _unique_username(user)
        assert result == "QuietOtter"

    def test_excludes_the_user_being_anonymized_from_the_collision_check(
        self, user, db
    ):
        user.username = "SwiftFalcon"
        user.save(update_fields=["username"])
        with patch(
            "accounts.services._pseudonym_candidate",
            return_value=("Swift", "Falcon"),
        ):
            result = _unique_username(user)
        assert result == "SwiftFalcon"


class TestUniqueEmail:
    def test_derives_slug_and_domain_from_username(self, user, db):
        result = _unique_email(user, "SwiftFalcon")
        assert result.startswith("swiftfalcon-")
        assert result.endswith("@deleted.invalid")

    def test_rerolls_on_collision(self, user, db):
        UserFactory(email="swiftfalcon-aaaaaa@deleted.invalid")
        with patch(
            "accounts.services.secrets.token_hex",
            side_effect=["aaaaaa", "bbbbbb"],
        ):
            result = _unique_email(user, "SwiftFalcon")
        assert result == "swiftfalcon-bbbbbb@deleted.invalid"


class TestAnonymizeAccount:
    def test_replaces_identity_fields_and_deactivates(self, user, db):
        user.email = "real.person@example.com"
        user.display_name = "Real Person"
        user.oidc_sub = "oidc-subject-123"
        user.liftosaur_api_key = "a-real-api-key"
        original_username = user.username
        user.save()

        anonymize_account(user)
        user.refresh_from_db()

        assert user.username != original_username
        assert user.display_name == user.username
        assert user.email != "real.person@example.com"
        assert user.email.endswith("@deleted.invalid")
        assert user.oidc_sub is None
        assert user.liftosaur_api_key is None
        assert user.is_active is False
        assert user.deactivated_at is not None

    def test_clears_every_connected_tracker_credential(self, user, db):
        # Wger/Hevy were added after this feature first shipped -- anonymize
        # only ever cleared liftosaur_api_key until this test caught that
        # deletion left a live Wger/Hevy credential on an otherwise-anonymized
        # account, matching what each tracker's own manual disconnect clears.
        user.liftosaur_api_key = "a-real-liftosaur-key"
        user.wger_instance_url = "https://my-wger.example.com"
        user.wger_api_token = "a-real-wger-token"
        user.hevy_api_key = "a-real-hevy-key"
        user.save()

        anonymize_account(user)
        user.refresh_from_db()

        assert user.liftosaur_api_key is None
        assert user.wger_instance_url is None
        assert user.wger_api_token is None
        assert user.hevy_api_key is None

    def test_deletes_avatar_file_from_storage(self, settings, tmp_path, user, db):
        settings.MEDIA_ROOT = tmp_path
        user.avatar = _png()
        user.save(update_fields=["avatar"])
        stored_path = Path(user.avatar.path)
        assert stored_path.exists()

        anonymize_account(user)
        user.refresh_from_db()

        assert not user.avatar
        assert not stored_path.exists()

    def test_participation_and_scoring_rows_are_untouched(self, user, db):
        challenge = make_custom_challenge(
            lifts=["Squat"], status=Challenge.Status.ACTIVE
        )
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        event = PointEarnEventFactory(
            user=user, challenge=challenge, lift="Squat", points_earned=5
        )

        anonymize_account(user)

        participant.refresh_from_db()
        event.refresh_from_db()
        assert participant.user_id == user.id
        assert event.user_id == user.id
        assert event.points_earned == 5


class TestDeleteAccountConfirmationForm:
    def test_exact_phrase_is_valid(self):
        form = DeleteAccountConfirmationForm(data={"confirmation": "delete my account"})
        assert form.is_valid()

    def test_is_case_and_whitespace_insensitive(self):
        form = DeleteAccountConfirmationForm(
            data={"confirmation": "  Delete My Account  "}
        )
        assert form.is_valid()

    def test_wrong_phrase_is_invalid(self):
        form = DeleteAccountConfirmationForm(data={"confirmation": "yes delete"})
        assert not form.is_valid()

    def test_blank_is_invalid(self):
        form = DeleteAccountConfirmationForm(data={"confirmation": ""})
        assert not form.is_valid()


class TestDeleteAccountView:
    def test_requires_login(self, client, db):
        response = client.get(reverse("accounts:delete-account"))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_get_renders_confirmation_page(self, authed_client, user):
        response = authed_client.get(reverse("accounts:delete-account"))
        assert response.status_code == 200
        assert "accounts/delete_account.html" in [t.name for t in response.templates]

    def test_no_picker_drawer_when_owning_nothing(self, authed_client, user):
        response = authed_client.get(reverse("accounts:delete-account"))
        assert b"needs a new owner" not in response.content

    def test_wrong_confirmation_does_not_anonymize(self, authed_client, user):
        original_username = user.username
        response = authed_client.post(
            reverse("accounts:delete-account"), {"confirmation": "nope"}
        )
        user.refresh_from_db()
        assert response.status_code == 200
        assert user.username == original_username
        assert user.is_active is True

    def test_correct_confirmation_anonymizes_and_logs_out(self, user, db):
        user.set_password("locallyauthed-pass-1")
        user.save()
        c = Client()
        # force_login after set_password: Django's session auth hash is
        # derived from the password, so logging in before changing it would
        # make this session's own next request look stale (get_user()
        # silently treats a hash-mismatched session as anonymous).
        c.force_login(user)
        original_username = user.username

        response = c.post(
            reverse("accounts:delete-account"), {"confirmation": "delete my account"}
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login")
        user.refresh_from_db()
        assert user.username != original_username
        assert user.is_active is False

        # The session was actually logged out, not just the account flagged.
        dashboard_response = c.get(reverse("challenges:dashboard"))
        assert dashboard_response.status_code == 302
        assert "/accounts/login/" in dashboard_response["Location"]

    def test_works_for_oidc_only_account_with_no_usable_password(self, user, db):
        user.set_unusable_password()
        user.oidc_sub = "some-oidc-subject"
        assert not user.has_usable_password()
        user.save(update_fields=["oidc_sub", "password"])
        c = Client()
        c.force_login(user)

        response = c.post(
            reverse("accounts:delete-account"), {"confirmation": "delete my account"}
        )

        assert response.status_code == 302
        user.refresh_from_db()
        assert user.is_active is False
        assert user.oidc_sub is None

    def test_anonymized_local_account_can_no_longer_log_in(self, user, db):
        user.set_password("a-strong-local-pass-1")
        user.save()
        anonymize_account(user)

        c = Client()
        assert not c.login(username=user.username, password="a-strong-local-pass-1")


class TestDeleteAccountOwnershipHandoff:
    """A challenge the deleting user still owns is handed off first (#46
    follow-up) -- otherwise it'd be stranded behind a creator who can never
    log back in. transfer_ownership itself is unit-tested separately
    (test_transfer_ownership.py); these cover the view's default/override/
    fallback wiring specifically."""

    def test_picker_shown_and_transfers_to_default_when_untouched(self, user, db):
        comp = make_custom_challenge(status=Challenge.Status.ACTIVE, creator=user)
        older = ChallengeParticipantFactory(
            challenge=comp,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=datetime(2026, 1, 1, tzinfo=UTC),
        ).user
        ChallengeParticipantFactory(
            challenge=comp,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=datetime(2026, 1, 10, tzinfo=UTC),
        )
        c = Client()
        c.force_login(user)

        get_response = c.get(reverse("accounts:delete-account"))
        assert b"needs a new owner" in get_response.content

        # Submitted with no new_owner__<pk> field at all -- simulates never
        # opening the picker drawer.
        c.post(
            reverse("accounts:delete-account"), {"confirmation": "delete my account"}
        )

        comp.refresh_from_db()
        assert comp.creator_id == older.id

    def test_picker_honours_an_explicit_override(self, user, db):
        comp = make_custom_challenge(status=Challenge.Status.ACTIVE, creator=user)
        older = ChallengeParticipantFactory(
            challenge=comp,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=datetime(2026, 1, 1, tzinfo=UTC),
        ).user
        newer = ChallengeParticipantFactory(
            challenge=comp,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=datetime(2026, 1, 10, tzinfo=UTC),
        ).user
        c = Client()
        c.force_login(user)

        c.post(
            reverse("accounts:delete-account"),
            {
                "confirmation": "delete my account",
                f"new_owner__{comp.pk}": str(newer.id),
            },
        )

        comp.refresh_from_db()
        assert comp.creator_id == newer.id
        assert comp.creator_id != older.id

    def test_tampered_choice_falls_back_to_default_instead_of_erroring(self, user, db):
        comp = make_custom_challenge(status=Challenge.Status.ACTIVE, creator=user)
        older = ChallengeParticipantFactory(
            challenge=comp,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=datetime(2026, 1, 1, tzinfo=UTC),
        ).user
        outsider = UserFactory()
        c = Client()
        c.force_login(user)

        response = c.post(
            reverse("accounts:delete-account"),
            {
                "confirmation": "delete my account",
                # Not one of this challenge's actual eligible candidates.
                f"new_owner__{comp.pk}": str(outsider.id),
            },
        )

        assert response.status_code == 302
        comp.refresh_from_db()
        assert comp.creator_id == older.id

    def test_new_owner_is_notified(self, user, db):
        comp = make_custom_challenge(status=Challenge.Status.ACTIVE, creator=user)
        successor = ChallengeParticipantFactory(
            challenge=comp, invite_status=ChallengeParticipant.InviteStatus.ACCEPTED
        ).user
        c = Client()
        c.force_login(user)

        c.post(
            reverse("accounts:delete-account"), {"confirmation": "delete my account"}
        )

        assert Notification.objects.filter(
            user=successor,
            challenge=comp,
            event_type=Notification.EventType.OWNERSHIP_TRANSFERRED,
        ).exists()

    def test_terminal_challenge_is_left_alone(self, user, db):
        comp = make_custom_challenge(status=Challenge.Status.COMPLETED, creator=user)
        c = Client()
        c.force_login(user)

        c.post(
            reverse("accounts:delete-account"), {"confirmation": "delete my account"}
        )

        comp.refresh_from_db()
        assert comp.creator_id == user.id


class TestSettingsDangerZone:
    def test_delete_account_link_present(self, authed_client, user):
        response = authed_client.get(reverse("accounts:settings"))
        assert reverse("accounts:delete-account").encode() in response.content


class TestAnonymizedIdentityIsReusable:
    """AC#5: a deleted account's original username/email must be free for a
    new registration -- verifies anonymize_account doesn't leave the old
    unique username around (nor a lingering email collision, though email has
    no unique constraint at the model level)."""

    def test_original_username_available_for_new_registration(self, client, db):
        original = UserFactory(username="reusablehandle")
        anonymize_account(original)

        response = client.post(
            reverse("accounts:register"),
            {
                "username": "reusablehandle",
                "password": "a-brand-new-pass-1",
                "password_confirm": "a-brand-new-pass-1",
                "accept_terms": "on",
            },
        )

        assert response.status_code == 302
        assert User.objects.filter(username="reusablehandle", is_active=True).exists()
