"""Tests for the forgot-password email recovery flow (TASK-283).

``mail.outbox`` works without any EMAIL_BACKEND override here: pytest-django's
session fixture calls ``django.test.utils.setup_test_environment()``, which
swaps in the locmem backend and creates the outbox.
"""

import logging
import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import (
    PasswordResetTokenGenerator,
    default_token_generator,
)
from django.core import mail
from django.core.cache import caches
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from accounts.services import send_password_reset_email
from accounts.timezones import DETECT_COOKIE_NAME

User = get_user_model()

STRONG_PASSWORD = "Kettlebell-Swing-91"


def local_user(email="lifter@example.com", password="Deadlift-Season-42", **kwargs):
    """A live local account: usable password plus an email address."""
    from accounts.tests.factories import UserFactory

    user = UserFactory(email=email, **kwargs)
    user.set_password(password)
    user.save()
    return user


def oidc_user(email="sso@example.com", **kwargs):
    """An account as OIDCBackend.create_user leaves it: no usable password."""
    from accounts.tests.factories import UserFactory

    user = UserFactory(email=email, **kwargs)
    user.set_unusable_password()
    user.save()
    return user


def _without_csrf_token(content: bytes) -> bytes:
    """Blank the CSRF token base.html puts in its body hx-headers attribute.

    Django re-masks the token with a fresh random salt on every render, so it
    differs even between two requests in one session. It is the only
    legitimately per-render byte on these pages; everything else must match.
    """
    return re.sub(rb'"X-CSRFToken": "[^"]*"', b'"X-CSRFToken": ""', content)


def confirm_url(user):
    return reverse(
        "accounts:password-reset-confirm",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        },
    )


@pytest.fixture
def oidc_only(settings):
    settings.OIDC_ONLY_LOGIN = True
    settings.OIDC_RP_CLIENT_ID = "client-id"
    settings.OIDC_RP_CLIENT_SECRET = "client-secret"
    settings.OIDC_OP_AUTHORIZATION_ENDPOINT = "https://idp.example/authorize"
    settings.OIDC_OP_TOKEN_ENDPOINT = "https://idp.example/token"
    settings.OIDC_OP_USER_ENDPOINT = "https://idp.example/userinfo"
    settings.OIDC_OP_JWKS_ENDPOINT = "https://idp.example/jwks"
    return settings


@pytest.mark.django_db
class TestPasswordResetRequestView:
    def test_get_returns_200_with_reset_template(self):
        response = Client().get(reverse("accounts:password-reset"))
        assert response.status_code == 200
        assert "registration/password_reset.html" in [
            t.name for t in response.templates
        ]

    def test_authenticated_user_is_sent_to_the_dashboard(self):
        client = Client()
        client.force_login(local_user())
        response = client.get(reverse("accounts:password-reset"))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")

    def test_live_local_account_gets_a_reset_link(self):
        user = local_user(email="lifter@example.com")
        response = Client().post(
            reverse("accounts:password-reset"), {"email": "lifter@example.com"}
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:password-reset-done")
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["lifter@example.com"]
        assert "/accounts/reset/" in mail.outbox[0].body
        assert user.username in mail.outbox[0].body

    def test_the_email_body_stays_ascii(self):
        """Django encodes every utf-8 text part as quoted-printable, which
        soft-wraps at 76 columns — mail clients rejoin those breaks, but the
        console backend's raw output (the documented recovery path on a
        deployment with no relay) is read by a human. Keeping the prose ASCII
        means only the long URL line carries QP artefacts instead of every
        sentence with punctuation in it."""
        local_user(email="lifter@example.com")
        Client().post(
            reverse("accounts:password-reset"), {"email": "lifter@example.com"}
        )
        mail.outbox[0].body.encode("ascii")

    def test_the_emailed_link_actually_works(self):
        local_user(email="lifter@example.com")
        client = Client()
        client.post(reverse("accounts:password-reset"), {"email": "lifter@example.com"})
        link = [
            word for word in mail.outbox[0].body.split() if "/accounts/reset/" in word
        ][0]
        path = link.split("testserver", 1)[1] if "testserver" in link else link
        assert client.get(path).status_code == 200

    def test_address_lookup_is_case_insensitive(self):
        local_user(email="Lifter@Example.com")
        Client().post(
            reverse("accounts:password-reset"), {"email": "lifter@example.com"}
        )
        assert len(mail.outbox) == 1

    def test_unknown_address_sends_nothing(self):
        response = Client().post(
            reverse("accounts:password-reset"), {"email": "nobody@example.com"}
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:password-reset-done")
        assert mail.outbox == []

    def test_oidc_only_account_sends_nothing(self):
        oidc_user(email="sso@example.com")
        response = Client().post(
            reverse("accounts:password-reset"), {"email": "sso@example.com"}
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:password-reset-done")
        assert mail.outbox == []

    def test_deactivated_local_account_sends_nothing(self):
        local_user(email="gone@example.com", is_active=False)
        Client().post(reverse("accounts:password-reset"), {"email": "gone@example.com"})
        assert mail.outbox == []

    def test_shared_address_only_mails_the_resettable_account(self):
        """User.email is not unique, so a hit can be 0, 1, or many accounts."""
        resettable = local_user(email="shared@example.com", username="local-one")
        oidc_user(email="shared@example.com", username="sso-one")
        Client().post(
            reverse("accounts:password-reset"), {"email": "shared@example.com"}
        )
        assert len(mail.outbox) == 1
        assert resettable.username in mail.outbox[0].body

    def test_two_local_accounts_sharing_an_address_both_get_mail(self):
        local_user(email="shared@example.com", username="local-a")
        local_user(email="shared@example.com", username="local-b")
        Client().post(
            reverse("accounts:password-reset"), {"email": "shared@example.com"}
        )
        assert len(mail.outbox) == 2
        # The username is what makes two mails to one inbox distinguishable.
        bodies = "".join(message.body for message in mail.outbox)
        assert "local-a" in bodies
        assert "local-b" in bodies

    @pytest.mark.parametrize("submitted", ["notanemail", "", "   "])
    def test_malformed_address_re_renders_with_an_error(self, submitted):
        response = Client().post(
            reverse("accounts:password-reset"), {"email": submitted}
        )
        assert response.status_code == 200
        assert response.context["errors"]["email"]
        assert mail.outbox == []

    def test_smtp_failure_looks_exactly_like_success(self, caplog):
        """An uncaught send error would 500 for a real address while a
        nonexistent one rendered the done page -- an existence oracle."""
        local_user(email="lifter@example.com")
        with patch("accounts.services.send_mail", side_effect=OSError("relay refused")):
            response = Client().post(
                reverse("accounts:password-reset"), {"email": "lifter@example.com"}
            )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:password-reset-done")
        assert "Password reset email send failed" in caplog.text

    def test_the_email_never_logs_the_address_or_token(self, caplog):
        user = local_user(email="lifter@example.com")
        with caplog.at_level(logging.INFO, logger="accounts.services"):
            Client().post(
                reverse("accounts:password-reset"), {"email": "lifter@example.com"}
            )
        assert str(user.id) in caplog.text
        # A reset token is a bearer credential and the address is the thing the
        # flow refuses to confirm; neither belongs in a log line.
        assert "lifter@example.com" not in caplog.text
        assert "/accounts/reset/" not in caplog.text


@pytest.mark.django_db
class TestSendPasswordResetEmailService:
    """The service is also callable directly (a shell, a future admin action),
    so its own guards are tested rather than only reached through the view."""

    @pytest.mark.parametrize("address", ["", "   ", None])
    def test_blank_address_is_a_no_op(self, address):
        local_user(email="lifter@example.com")
        send_password_reset_email(address, base_url="https://example.test")
        assert mail.outbox == []

    def test_base_url_is_used_verbatim_in_the_link(self):
        local_user(email="lifter@example.com")
        send_password_reset_email(
            "lifter@example.com", base_url="https://verylift.example"
        )
        assert "https://verylift.example/accounts/reset/" in mail.outbox[0].body

    def test_renders_in_the_recipients_pinned_language_not_the_default(self):
        # There is no request/browser Accept-Language available at send time
        # (the reader may open this on a different device days later), so the
        # stored per-account preference is what must drive it, not the site
        # default (English) and not whatever LocaleMiddleware guessed for the
        # anonymous requester who submitted the form.
        local_user(email="lifter@example.com", language="es")
        send_password_reset_email("lifter@example.com", base_url="https://example.test")
        sent = mail.outbox[0]
        assert sent.subject == "Restablece tu contraseña de very lift"
        assert "Establece una contraseña nueva:" in sent.body

    def test_falls_back_to_site_default_when_language_is_unset(self):
        # "" is User.language's "automatic" value (see accounts/models.py) --
        # must not crash translation.override and must not silently emit the
        # empty-string pseudo-locale.
        local_user(email="lifter@example.com", language="")
        send_password_reset_email("lifter@example.com", base_url="https://example.test")
        assert mail.outbox[0].subject == "Reset your very lift password"


@pytest.mark.django_db
class TestPasswordResetEnumerationSafety:
    """AC #5: the three outcomes must be indistinguishable to the requester."""

    def test_all_three_cases_produce_identical_responses(self):
        local_user(email="real@example.com")
        oidc_user(email="sso@example.com")

        client = Client()
        results = []
        for address in ("real@example.com", "sso@example.com", "nobody@example.com"):
            response = client.post(
                reverse("accounts:password-reset"), {"email": address}
            )
            done = client.get(response["Location"])
            results.append(
                (
                    response.status_code,
                    response["Location"],
                    _without_csrf_token(done.content),
                )
            )

        assert results[0] == results[1] == results[2]
        # Only the real account produced mail, and the requester cannot tell.
        assert len(mail.outbox) == 1

    def test_done_page_does_not_echo_the_submitted_address(self):
        client = Client()
        client.post(reverse("accounts:password-reset"), {"email": "real@example.com"})
        done = client.get(reverse("accounts:password-reset-done"))
        assert b"real@example.com" not in done.content


@pytest.mark.django_db
class TestPasswordResetConfirmView:
    def test_valid_link_renders_the_form(self):
        user = local_user()
        response = Client().get(confirm_url(user))
        assert response.status_code == 200
        assert response.context["validlink"] is True
        assert b'name="password"' in response.content

    def test_valid_post_sets_the_new_password(self):
        user = local_user()
        response = Client().post(
            confirm_url(user),
            {"password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login")
        user.refresh_from_db()
        assert user.check_password(STRONG_PASSWORD)

    def test_link_is_single_use(self):
        """AC #3. Needs no bookkeeping: the token hashes the stored password."""
        user = local_user()
        url = confirm_url(user)
        Client().post(
            url, {"password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD}
        )

        replay = Client().post(
            url,
            {"password": "Second-Attempt-77", "password_confirm": "Second-Attempt-77"},
        )
        assert replay.status_code == 200
        assert replay.context["validlink"] is False
        user.refresh_from_db()
        assert user.check_password(STRONG_PASSWORD)

    def test_expired_link_is_rejected(self, settings):
        """AC #3. _now is the documented mocking seam. Not PASSWORD_RESET_TIMEOUT=0:
        check_token compares strictly greater-than, so a same-second token would
        still validate and the test would pass for the wrong reason."""
        settings.PASSWORD_RESET_TIMEOUT = 3600
        user = local_user()
        url = confirm_url(user)
        later = timezone.now() + timedelta(seconds=3601)
        with patch.object(
            PasswordResetTokenGenerator, "_now", return_value=later.replace(tzinfo=None)
        ):
            response = Client().get(url)
        assert response.status_code == 200
        assert response.context["validlink"] is False

    def test_tampered_token_is_rejected(self):
        user = local_user()
        url = confirm_url(user).rsplit("/", 2)[0] + "/aaaaaa-bbbbbbbbbbbbbbbbbb/"
        response = Client().get(url)
        assert response.status_code == 200
        assert response.context["validlink"] is False

    def test_non_uuid_uidb64_does_not_500(self):
        """The UUID pk makes the queryset raise ValidationError, not ValueError."""
        url = reverse(
            "accounts:password-reset-confirm",
            kwargs={
                "uidb64": urlsafe_base64_encode(b"not-a-uuid"),
                "token": "aaaaaa-bbbbbbbbbbbbbbbbbb",
            },
        )
        response = Client().get(url)
        assert response.status_code == 200
        assert response.context["validlink"] is False

    def test_undecodable_uidb64_does_not_500(self):
        url = reverse(
            "accounts:password-reset-confirm",
            kwargs={"uidb64": "!!!not-base64!!!", "token": "aaaaaa-bbbbbbbbbbbbbbbbbb"},
        )
        response = Client().get(url)
        assert response.status_code == 200
        assert response.context["validlink"] is False

    def test_deactivated_account_link_is_rejected(self):
        user = local_user()
        url = confirm_url(user)
        user.is_active = False
        user.save()
        response = Client().get(url)
        assert response.context["validlink"] is False

    def test_oidc_only_account_link_is_rejected(self):
        user = local_user()
        url = confirm_url(user)
        user.set_unusable_password()
        user.save()
        response = Client().get(url)
        assert response.context["validlink"] is False

    def test_weak_password_is_rejected(self):
        user = local_user()
        response = Client().post(
            confirm_url(user), {"password": "12345", "password_confirm": "12345"}
        )
        assert response.status_code == 200
        assert response.context["errors"]["password"]
        user.refresh_from_db()
        assert not user.check_password("12345")

    def test_mismatched_confirmation_is_rejected(self):
        user = local_user()
        response = Client().post(
            confirm_url(user),
            {"password": STRONG_PASSWORD, "password_confirm": "Something-Else-31"},
        )
        assert response.status_code == 200
        assert response.context["errors"]["password"]
        user.refresh_from_db()
        assert not user.check_password(STRONG_PASSWORD)

    def test_reset_invalidates_other_sessions(self):
        """Django's session auth hash covers this for free on AbstractBaseUser --
        asserted rather than assumed, since a gap here would be a real
        session-fixation risk."""
        user = local_user()
        stale = Client()
        stale.force_login(user)
        assert stale.get(reverse("accounts:settings")).status_code == 200

        Client().post(
            confirm_url(user),
            {"password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD},
        )
        assert stale.get(reverse("accounts:settings")).status_code == 302

    def test_reset_link_survives_the_timezone_detour(self):
        """conftest seeds pp_tzdetect on every Client, so nothing else in the
        suite would notice UserTimezoneMiddleware eating an emailed link."""
        user = local_user()
        url = confirm_url(user)
        client = Client()
        del client.cookies[DETECT_COOKIE_NAME]

        detour = client.get(url)
        assert detour.status_code == 302
        assert detour["Location"].startswith(reverse("accounts:timezone-detect"))
        assert (
            url in detour["Location"] or url.replace("/", "%2F") in detour["Location"]
        )

        followed = client.get(url, {"tzdetect": "1"})
        assert followed.status_code == 200
        assert followed.context["validlink"] is True


@pytest.mark.django_db
class TestPasswordResetOIDCOnlyMode:
    """AC #4: every endpoint closes, on POST as well as GET."""

    def test_request_get_redirects_to_the_provider(self, oidc_only):
        response = Client().get(reverse("accounts:password-reset"))
        assert response.status_code == 302
        assert response["Location"] == reverse("oidc_authentication_init")

    def test_request_get_never_renders_the_reset_template(self, oidc_only):
        response = Client().get(reverse("accounts:password-reset"))
        assert "registration/password_reset.html" not in [
            t.name for t in response.templates
        ]

    def test_request_post_sends_no_mail(self, oidc_only):
        local_user(email="lifter@example.com")
        response = Client().post(
            reverse("accounts:password-reset"), {"email": "lifter@example.com"}
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("oidc_authentication_init")
        assert mail.outbox == []

    def test_done_page_redirects_to_the_provider(self, oidc_only):
        response = Client().get(reverse("accounts:password-reset-done"))
        assert response.status_code == 302
        assert response["Location"] == reverse("oidc_authentication_init")

    def test_confirm_get_redirects_to_the_provider(self, settings, oidc_only):
        user = local_user()
        settings.OIDC_ONLY_LOGIN = True
        response = Client().get(confirm_url(user))
        assert response.status_code == 302
        assert response["Location"] == reverse("oidc_authentication_init")

    def test_confirm_post_leaves_the_password_alone(self, oidc_only):
        user = local_user(password="Original-Password-19")
        response = Client().post(
            confirm_url(user),
            {"password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("oidc_authentication_init")
        user.refresh_from_db()
        assert user.check_password("Original-Password-19")


@pytest.mark.django_db
class TestPasswordResetRateLimiting:
    """The rate limits are what bound the residual timing side-channel, so they
    are part of the AC #5 story rather than incidental hardening."""

    @pytest.fixture
    def _enable_ratelimit(self, settings):
        settings.RATELIMIT_ENABLE = True
        caches["ratelimit"].clear()
        with patch("django_ratelimit.core.time") as mock_time:
            mock_time.time.return_value = 1_700_000_000
            yield
        caches["ratelimit"].clear()

    def test_per_ip_limit_blocks_with_429(self, settings, _enable_ratelimit):
        settings.RATELIMIT_PASSWORD_RESET_IP = "2/h"
        settings.RATELIMIT_PASSWORD_RESET_EMAIL = "100/h"
        client = Client()
        url = reverse("accounts:password-reset")
        for index in range(2):
            assert (
                client.post(url, {"email": f"a{index}@example.com"}).status_code == 302
            )
        assert client.post(url, {"email": "a3@example.com"}).status_code == 429

    def test_per_address_limit_blocks_with_429(self, settings, _enable_ratelimit):
        settings.RATELIMIT_PASSWORD_RESET_IP = "100/h"
        settings.RATELIMIT_PASSWORD_RESET_EMAIL = "2/h"
        url = reverse("accounts:password-reset")
        for _ in range(2):
            assert Client().post(url, {"email": "same@example.com"}).status_code == 302
        assert Client().post(url, {"email": "same@example.com"}).status_code == 429
