"""Service functions for the accounts app."""

import logging
import secrets
import uuid
from io import BytesIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from PIL import Image, ImageOps

from core.bodyweight import TrackerBodyweight
from hevy_api import services as hevy_services
from liftosaur import services as liftosaur_services
from wger import services as wger_services

logger = logging.getLogger(__name__)

AVATAR_MAX_DIMENSION = 1024
AVATAR_AVIF_QUALITY = 70
AVATAR_AVIF_SPEED = 3


def transcode_avatar_to_avif(uploaded):
    """Normalise an uploaded avatar to a downscaled, EXIF-stripped AVIF.

    Returns a ``ContentFile`` ready to assign to ``User.avatar``, or ``None``
    when the caller should keep the original file. A missing or broken codec
    must degrade to "the stored avatar is bigger", never to "the user cannot
    set a profile photo".

    No ``mimetypes.add_type`` is needed for the result: ``.avif`` is in the
    stdlib map that ``django.views.static.serve`` consults on both Python 3.12
    and 3.14. (``.webp`` is not, so a future WebP variant would differ here.)
    """
    try:
        # Django's ImageField.to_python has already read the stream.
        uploaded.seek(0)
        buffer = BytesIO()
        with Image.open(uploaded) as source:
            # thumbnail() before exif_transpose(), not after: thumbnail() calls
            # draft() internally so a large JPEG decodes at 1/2, 1/4 or 1/8
            # scale, while exif_transpose() forces a full load and would
            # forfeit that (205 MB vs 146 MB peak RSS on a 24 Mpx photo). The
            # thumbnail box is square, so the resize and the rotation commute.
            source.thumbnail(
                (AVATAR_MAX_DIMENSION, AVATAR_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            # Pillow's AVIF writer only emits EXIF when passed exif=, so the
            # orientation tag is dropped on save. Bake the rotation into the
            # pixels first or portrait phone photos store sideways.
            image = ImageOps.exif_transpose(source)
            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )
            image = image.convert("RGBA" if has_alpha else "RGB")
            image.save(
                buffer,
                format="AVIF",
                quality=AVATAR_AVIF_QUALITY,
                speed=AVATAR_AVIF_SPEED,
            )
    except Exception:
        logger.exception(
            "AVIF transcode failed for avatar upload %s",
            getattr(uploaded, "name", "?"),
        )
        return None
    # A uuid name, never a fixed avatar.avif: AvatarForm.save deletes the old
    # file before assigning the new one, so a fixed name would be reused and
    # browsers would keep serving the previous photo from cache.
    return ContentFile(buffer.getvalue(), name=f"{uuid.uuid4().hex}.avif")


def send_password_reset_email(email: str, *, base_url: str) -> None:
    """Mail a password-reset link to every eligible account at ``email``.

    Returns None unconditionally and never raises: the caller must not be able
    to learn whether anything was sent, because that answer is exactly the
    account-existence oracle the flow is designed not to give. An SMTP failure
    is logged and swallowed for the same reason -- letting it propagate would
    500 for a real address while a nonexistent one rendered the success page.

    ``User.email`` is not unique (and the OIDC backend copies the claim
    verbatim), so 0, 1, or many accounts can match; every eligible one is
    mailed, as Django's own PasswordResetForm does. Eligibility is
    ``is_active`` plus ``has_usable_password()`` -- the latter is what already
    distinguishes a local account from an OIDC-created one in this codebase
    (UserManager.create_user calls set_unusable_password() when no password is
    given, and OIDCBackend.create_user never passes one), and it is the actual
    precondition: a password that cannot be used cannot be reset.

    Takes ``base_url`` rather than ``request`` to keep this module request-free.
    """
    normalized = (email or "").strip()
    if not normalized:
        return

    User = get_user_model()
    # has_usable_password() can't be a queryset filter -- the unusable marker is
    # a hash-prefix convention, not a column -- so it filters in Python.
    candidates = [
        user
        for user in User.objects.filter(email__iexact=normalized, is_active=True)
        if user.has_usable_password()
    ]

    for user in candidates:
        path = reverse(
            "accounts:password-reset-confirm",
            kwargs={
                "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
                "token": default_token_generator.make_token(user),
            },
        )
        context = {"username": user.username, "reset_url": f"{base_url}{path}"}
        # Render in the recipient's own pinned preference, not the requesting
        # browser's Accept-Language -- the person submitting the form may not
        # be the eventual reader (a different device, a shared computer), and
        # unlike every page in the app there is no live request to negotiate
        # against once the email is actually opened. An empty User.language
        # ("automatic") falls back to the site default, same as everywhere else.
        with translation.override(user.language or settings.LANGUAGE_CODE):
            # Collapse newlines: a subject template ending in one would
            # otherwise become a header injection point.
            subject = "".join(
                render_to_string(
                    "registration/password_reset_email_subject.txt", context
                ).splitlines()
            )
            body = render_to_string(
                "registration/password_reset_email_body.txt", context
            )
        try:
            send_mail(
                subject,
                body,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=False,
            )
        except Exception:
            # Never log the address or the token -- a reset token is a bearer
            # credential. user.id is enough to correlate with the send below.
            logger.exception("Password reset email send failed for user %s", user.id)
            continue
        logger.info("Password reset email sent for user %s", user.id)


def mask_api_key(key: str | None) -> str | None:
    """Bullets plus the last 6 characters of a stored secret, or None if unset.

    Shared by the settings page and the Django admin so the two surfaces cannot
    drift into showing different amounts of a key.
    """
    if not key:
        return None
    visible = key[-6:] if len(key) >= 6 else key
    return "•" * 8 + visible


# Deliberately a "SwiftFalcon"-style pool (GitHub/Slack/Docker convention), not
# an obviously-synthetic value like "deleted-user-8f2a1c" -- see
# anonymize_account's docstring for why that distinction matters for TASK-308.
_PSEUDONYM_ADJECTIVES = [
    "Amber",
    "Bold",
    "Brave",
    "Bright",
    "Calm",
    "Clever",
    "Cosmic",
    "Crimson",
    "Daring",
    "Eager",
    "Fleet",
    "Frosty",
    "Gentle",
    "Golden",
    "Happy",
    "Hardy",
    "Jolly",
    "Keen",
    "Lively",
    "Lucky",
    "Merry",
    "Mighty",
    "Nimble",
    "Northern",
    "Proud",
    "Quick",
    "Quiet",
    "Rapid",
    "Sleek",
    "Silent",
    "Steady",
    "Sunny",
    "Swift",
    "Tidy",
    "Vivid",
    "Witty",
    "Zesty",
]
_PSEUDONYM_NOUNS = [
    "Badger",
    "Beetle",
    "Cougar",
    "Dolphin",
    "Egret",
    "Falcon",
    "Ferret",
    "Gopher",
    "Heron",
    "Ibis",
    "Jackal",
    "Kestrel",
    "Lynx",
    "Loon",
    "Mantis",
    "Marten",
    "Newt",
    "Osprey",
    "Otter",
    "Panda",
    "Quokka",
    "Raven",
    "Sparrow",
    "Tapir",
    "Urchin",
    "Viper",
    "Walrus",
    "Wombat",
    "Yak",
    "Zebra",
]

ANONYMIZED_EMAIL_DOMAIN = "deleted.invalid"

# 36 adjectives x 30 nouns already gives 1,080 combinations; this many misses
# in a row would mean the account pool has grown enormous, at which point
# falling through to an entropy-padded value is the right degrade rather than
# looping forever.
_MAX_PSEUDONYM_ATTEMPTS = 50


def _pseudonym_candidate() -> tuple[str, str]:
    return secrets.choice(_PSEUDONYM_ADJECTIVES), secrets.choice(_PSEUDONYM_NOUNS)


def _unique_username(user) -> str:
    User = get_user_model()
    for _ in range(_MAX_PSEUDONYM_ATTEMPTS):
        adjective, noun = _pseudonym_candidate()
        candidate = f"{adjective}{noun}"
        if not User.objects.exclude(pk=user.pk).filter(username=candidate).exists():
            return candidate
    adjective, noun = _pseudonym_candidate()
    return f"{adjective}{noun}{secrets.token_hex(4)}"


def _unique_email(user, username: str) -> str:
    User = get_user_model()
    slug = username.lower()
    for _ in range(_MAX_PSEUDONYM_ATTEMPTS):
        candidate = f"{slug}-{secrets.token_hex(3)}@{ANONYMIZED_EMAIL_DOMAIN}"
        taken = User.objects.exclude(pk=user.pk).filter(email__iexact=candidate)
        if not taken.exists():
            return candidate
    return f"{slug}-{secrets.token_hex(8)}@{ANONYMIZED_EMAIL_DOMAIN}"


def anonymize_account(user) -> None:
    """Anonymize ``user`` in place and mark the account inactive (#46, TASK-308).

    This is the self-serve "Delete account" flow's entire effect -- there is no
    accompanying hard delete. ``ChallengeParticipant``, scoring rows, and
    ``PolicyConsent`` are untouched; only identity fields on the ``User`` row
    itself change. Every display site that used to special-case
    ``is_active=False`` with a separate "Former Participant" placeholder
    (scoring/services.py, challenges/views.py) now calls
    ``User.effective_display_name`` instead, which shows the pseudonym below
    with a "(deleted)" suffix -- a bare pseudonym without that suffix read as
    an unexplained stranger next to real names on the same leaderboard/chart,
    and the two pages disagreeing with each other (one showing the pseudonym,
    the other "Former Participant") was the actual bug report that prompted
    unifying on a single property.

    ``username``/``display_name`` become a random adjective-noun pseudonym
    (re-rolled on a uniqueness collision), deliberately not an obviously
    synthetic value like ``deleted-user-8f2a1c``: a normal-looking name blends
    into any leaderboard/challenge history it still appears in, where a
    visibly-synthetic one still flags "this used to be someone" and invites
    the reader to wonder who. ``email`` is derived from the same pseudonym so
    a support/audit trail can still associate a placeholder with the row it
    replaced, without being identifying itself.

    ``bodyweight_kg`` (and its source/timestamp) is cleared for the same
    reason ``avatar`` is: it is health-adjacent information about the person
    behind the row, not a record of anything that happened in a challenge.
    Nothing else depends on it -- scoring never read it, and a goal chart
    that was suggested from it stores its own frozen figure in
    ``CustomGoal.source_detail`` -- so clearing it strands no history. This
    is not an exception to the no-hard-deletes rule: the row itself survives
    intact, exactly as it does for every other identity field here.

    ``avatar`` is deleted from storage (not just cleared from the field --
    matching AvatarForm.save's ``.delete(save=False)`` convention). ``oidc_sub``
    and every field in ``User.TRACKER_CREDENTIAL_FIELDS`` (currently
    ``liftosaur_api_key``, ``wger_instance_url``, ``wger_api_token``, and
    ``hevy_api_key``) are cleared so the row can never re-authenticate or
    resume a sync with any tracker. ``is_active=False`` blocks login (Django's
    ``ModelBackend`` already refuses inactive users); ``deactivated_at`` is
    stamped for the first time this field has ever been populated by anything
    other than admin action.

    Session invalidation is the caller's responsibility (``django.contrib.
    auth.logout`` in the view) -- this function only touches the row.
    """
    username = _unique_username(user)
    email = _unique_email(user, username)

    if user.avatar:
        user.avatar.delete(save=False)

    user.username = username
    user.display_name = username
    user.email = email
    user.avatar = None
    user.oidc_sub = None
    user.bodyweight_kg = None
    user.bodyweight_source = ""
    user.bodyweight_updated_at = None
    tracker_credential_fields = get_user_model().TRACKER_CREDENTIAL_FIELDS
    for field_name in tracker_credential_fields:
        setattr(user, field_name, None)
    user.is_active = False
    user.deactivated_at = timezone.now()
    user.save(
        update_fields=[
            "username",
            "display_name",
            "email",
            "avatar",
            "oidc_sub",
            "bodyweight_kg",
            "bodyweight_source",
            "bodyweight_updated_at",
            *tracker_credential_fields,
            "is_active",
            "deactivated_at",
        ]
    )
    logger.info("Account %s anonymized and deactivated", user.id)


def _tracker_bodyweight_readings(user) -> list[tuple[str, TrackerBodyweight]]:
    """Every bodyweight reading the account's connected trackers can offer.

    Each reader is independently fault-tolerant (returns ``None`` rather than
    raising), so one unreachable tracker never costs the lifter a reading
    another one could have supplied. A tracker the account has no credentials
    for is not called at all.
    """
    readings: list[tuple[str, TrackerBodyweight]] = []
    if user.liftosaur_api_key:
        reading = liftosaur_services.fetch_latest_bodyweight(user.liftosaur_api_key)
        if reading is not None:
            readings.append(("liftosaur", reading))
    if user.wger_instance_url and user.wger_api_token:
        reading = wger_services.fetch_latest_bodyweight(
            user.wger_instance_url, user.wger_api_token
        )
        if reading is not None:
            readings.append(("wger", reading))
    if user.hevy_api_key:
        reading = hevy_services.fetch_latest_bodyweight(user.hevy_api_key)
        if reading is not None:
            readings.append(("hevy", reading))
    return readings


def sync_bodyweight_from_trackers(user) -> bool:
    """Refresh ``user.bodyweight_kg`` from whichever trackers expose one.

    Every connected tracker is asked, and the reading with the most recent
    ``measured_at`` wins -- not the first tracker that answers. A lifter with
    two trackers connected typically weighs in on one of them, and picking by
    connection order would let a year-old figure from the other one win purely
    because that app sorts earlier in this function.

    The winning reading is stored only when it is genuinely newer than what
    is already on file. That is what keeps a hand-entered figure from being
    silently overwritten by a stale tracker measurement the lifter has
    already superseded -- while still letting a fresh weigh-in in the tracker
    replace a figure typed weeks ago, which is the whole point of syncing.
    Note this compares the tracker's measurement date against when the stored
    value was *written*; the two are the same thing for a manual entry, and
    for a tracker-sourced value the comparison is between two measurement
    dates only to within one sync's lag. That is accurate enough for a number
    whose only job is prefilling a goal suggestion, and the alternative --
    storing the measurement date as a fourth bodyweight column -- buys
    precision nothing in the product can spend.

    Returns whether the stored value changed. Never raises.
    """
    readings = _tracker_bodyweight_readings(user)
    if not readings:
        return False

    tracker, reading = max(readings, key=lambda item: item[1].measured_at)

    if user.bodyweight_updated_at is not None and (
        reading.measured_at <= user.bodyweight_updated_at
    ):
        logger.info(
            "Skipping %s bodyweight for user %s: stored value is newer",
            tracker,
            user.id,
        )
        return False

    User = get_user_model()
    user.set_bodyweight(reading.weight_kg, User.BodyweightSource.TRACKER)
    logger.info("Updated bodyweight for user %s from %s", user.id, tracker)
    return True
