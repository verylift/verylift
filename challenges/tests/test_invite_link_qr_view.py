"""Tests for the invite-link QR PNG endpoint (TASK-339 / issue #79)."""

from datetime import timedelta
from io import BytesIO

import pytest
import zxingcpp
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from challenges.models import Challenge
from challenges.services import build_invite_link_qr_png, regenerate_invite_link
from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory


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


def _decoded_payload(png_bytes: bytes) -> str:
    image = Image.open(BytesIO(png_bytes))
    [decoded] = zxingcpp.read_barcodes(image)
    return decoded.text


def _expected_invite_url(token: str) -> str:
    return f"http://testserver{reverse('challenges:invite-link', args=[token])}"


class TestQrRoundTrip:
    def test_decodes_back_to_the_same_url_the_copy_button_produces(self, link):
        # This subsumes a separate "is it a valid PNG" check: _decoded_payload
        # cannot return anything unless Pillow parsed the bytes as an image
        # and zxing found a barcode in it.
        response = Client().get(reverse("challenges:invite-link-qr", args=[link.token]))

        assert _decoded_payload(response.content) == _expected_invite_url(link.token)

    def test_post_is_rejected_without_rendering(self, link):
        # Read-only endpoint: a POST shouldn't build a PNG or burn a
        # rate-limit token.
        response = Client().post(
            reverse("challenges:invite-link-qr", args=[link.token])
        )

        assert response.status_code == 405


class TestUnknownOrDeadToken:
    def test_unknown_token_404s(self, db):
        response = Client().get(reverse("challenges:invite-link-qr", args=["nope"]))
        assert response.status_code == 404

    def test_expired_token_404s(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        response = Client().get(reverse("challenges:invite-link-qr", args=[link.token]))
        assert response.status_code == 404

    def test_revoked_token_404s(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        response = Client().get(reverse("challenges:invite-link-qr", args=[link.token]))
        assert response.status_code == 404

    def test_exhausted_token_404s(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
            max_uses=1,
            use_count=1,
        )
        response = Client().get(reverse("challenges:invite-link-qr", args=[link.token]))
        assert response.status_code == 404


class TestEndedChallengeStillHasALiveQr:
    def test_live_token_for_an_ended_challenge_still_renders(self, link):
        """A live-but-terminal link still resolves fine here; scanning it just
        lands on invite_link_view's own 'invite link ended' page, exactly like
        clicking the link directly would."""
        challenge = link.challenge
        challenge.status = Challenge.Status.COMPLETED
        challenge.save(update_fields=["status"])

        response = Client().get(reverse("challenges:invite-link-qr", args=[link.token]))

        assert response.status_code == 200
        assert _decoded_payload(response.content) == _expected_invite_url(link.token)


class TestRegeneration:
    def test_regenerating_kills_the_old_qr_without_touching_the_new_one(
        self, challenge
    ):
        old_link = ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        new_link = regenerate_invite_link(challenge, challenge.creator)

        old_response = Client().get(
            reverse("challenges:invite-link-qr", args=[old_link.token])
        )
        new_response = Client().get(
            reverse("challenges:invite-link-qr", args=[new_link.token])
        )

        assert old_response.status_code == 404
        assert new_response.status_code == 200
        assert _decoded_payload(new_response.content) == _expected_invite_url(
            new_link.token
        )


class TestCannotEnumerateTokens:
    def test_unknown_and_dead_tokens_are_indistinguishable(self, challenge):
        """Both 404 with no other signal, matching invite_link_view's own
        unknown-token handling -- a scan across many guesses can't tell
        "never existed" from "existed but died" any more than the join page
        already lets it."""
        dead_link = ChallengeInviteLinkFactory(
            challenge=challenge, revoked_at=timezone.now()
        )
        unknown_response = Client().get(
            reverse("challenges:invite-link-qr", args=["totally-made-up"])
        )
        dead_response = Client().get(
            reverse("challenges:invite-link-qr", args=[dead_link.token])
        )
        assert unknown_response.status_code == dead_response.status_code == 404


class TestBrandedQrStaysScannable:
    """The centred logo covers modules outright, so the code decodes only
    because error correction H can reconstruct what it hides. That makes two
    otherwise-innocuous edits silently destructive -- lowering the correction
    level, or growing _LOGO_WIDTH_RATIO -- because the result still looks
    exactly like a QR code and fails only on a real scanner.

    Parametrised by URL length because the risk scales with it: a longer URL
    packs more modules into the same image, so each one is smaller and the
    fixed-ratio logo swallows proportionally more data.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://vl.ca/join/AbCdEf12/",
            "https://verylift.ca/join/AbCdEf12/",
            "https://gigaproficiency.jul3s.ca/join/AbCdEf12/",
            "https://a-rather-long-subdomain.verylift.example.com/join/AbCdEf12/",
        ],
    )
    def test_decodes_at_every_realistic_url_length(self, url):
        assert _decoded_payload(build_invite_link_qr_png(url)) == url
