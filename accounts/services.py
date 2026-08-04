"""Service functions for the accounts app."""

import logging
import uuid
from io import BytesIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from PIL import Image, ImageOps

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
