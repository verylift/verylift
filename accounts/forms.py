"""Forms for the accounts app."""

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils.translation import gettext_lazy as _

from accounts.services import transcode_avatar_to_avif
from accounts.timezones import is_valid_timezone


class RegistrationForm(UserCreationForm):
    """Username + password form for self-serve registration.

    Subclasses UserCreationForm so AUTH_PASSWORD_VALIDATORS run automatically
    (the plain view previously only checked non-empty + confirm-match). Error
    messages are kept aligned with the existing registration copy. The
    Liftosaur API key is validated by the view because the key check hits an
    external API and must run only after the cheap checks pass.
    """

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        # email is optional (User.email is blank=True, so the form field follows)
        # and deliberately not checked for uniqueness -- User.email has no unique
        # constraint, the OIDC backend copies the claim verbatim, and
        # send_password_reset_email already handles a shared address by mailing
        # every eligible match.
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.error_messages["password_mismatch"] = _("Passwords do not match.")
        self.fields["username"].error_messages["required"] = _("Username is required.")
        self.fields["password1"].error_messages["required"] = _("Password is required.")

    def clean_username(self):
        username = self.cleaned_data.get("username")
        if (
            username
            and self._meta.model.objects.filter(username__iexact=username).exists()
        ):
            self.add_error(
                "username",
                _("That username is already taken."),
            )
            return None
        return username


class PasswordResetRequestForm(forms.Form):
    """The single address field on the forgot-password screen.

    Address-shape validation is surfaced to the user on purpose and is not an
    enumeration leak: "is this string a valid email address" is knowable
    client-side and says nothing about any account. Silently pretending to send
    for "notanemail" would just make a typo unrecoverable.
    """

    email = forms.EmailField()


class EmailForm(forms.Form):
    """Settings email form. A blank submission clears the stored address, same
    contract as NicknameForm -- an account with no email simply has no
    self-serve password recovery."""

    email = forms.EmailField(required=False)

    def save(self, user):
        user.email = self.cleaned_data["email"]
        user.save(update_fields=["email"])


class NicknameForm(forms.Form):
    """Settings display-name form. CharField strips surrounding whitespace and
    treats a blank submission as clearing the nickname."""

    display_name = forms.CharField(max_length=150, required=False)

    def save(self, user):
        user.display_name = self.cleaned_data["display_name"]
        user.save(update_fields=["display_name"])


class LiftosaurKeyForm(forms.Form):
    """Settings Liftosaur-key form. Saving only happens when a key is supplied."""

    liftosaur_api_key = forms.CharField(required=False)

    def save(self, user) -> bool:
        """Persist the key when present. Returns whether a key was saved."""
        api_key = self.cleaned_data["liftosaur_api_key"]
        if not api_key:
            return False
        user.liftosaur_api_key = api_key
        user.save(update_fields=["liftosaur_api_key"])
        return True


MAX_AVATAR_BYTES = 10 * 1024 * 1024
MAX_AVATAR_PIXELS = 25_000_000


class AvatarImageField(forms.ImageField):
    """ImageField that bounds what the server is willing to decode.

    Both checks live here rather than in ``AvatarForm.clean_avatar`` because
    ``ImageField.to_python`` is where Django itself calls ``Image.open()`` and
    ``verify()`` -- a field-level ``clean_<name>`` hook runs after the file has
    already been decoded, which is too late to be a guard.

    The byte cap alone is not enough: a 68-byte PNG whose IHDR declares
    11000x11000 sails through it and then costs hundreds of MB to rasterise,
    because Pillow only *raises* above twice its ``MAX_IMAGE_PIXELS``. The pixel
    cap closes that window using the PIL object Django has already attached, so
    it costs no second decode.
    """

    def to_python(self, data):
        if data and getattr(data, "size", 0) > MAX_AVATAR_BYTES:
            raise forms.ValidationError(_("Image is too large (max 10 MB)."))
        f = super().to_python(data)
        if f is not None and f.image.width * f.image.height > MAX_AVATAR_PIXELS:
            raise forms.ValidationError(
                _("Image dimensions are too large (max 25 megapixels).")
            )
        return f


class AvatarForm(forms.Form):
    """Settings profile-photo form. An uploaded file replaces any existing
    photo; the clear checkbox removes it and reverts to the initials avatar.

    Uploads are capped at 10 MB and 25 megapixels, then normalised to AVIF at
    a maximum of 1024px with EXIF (including GPS) stripped. A transcode failure
    falls back to storing the file as uploaded.
    """

    avatar = AvatarImageField(required=False)
    clear_avatar = forms.BooleanField(required=False)

    def clean_avatar(self):
        avatar = self.cleaned_data.get("avatar")
        if not avatar:
            return avatar
        return transcode_avatar_to_avif(avatar) or avatar

    def save(self, user):
        if self.cleaned_data.get("clear_avatar"):
            if user.avatar:
                user.avatar.delete(save=False)
            user.avatar = None
            user.save(update_fields=["avatar"])
        elif self.cleaned_data.get("avatar"):
            if user.avatar:
                user.avatar.delete(save=False)
            user.avatar = self.cleaned_data["avatar"]
            user.save(update_fields=["avatar"])


class UnitPreferenceForm(forms.Form):
    """Settings unit-preference form.

    Governs the display unit (kg/lb) for weights across the whole app, so it
    lives in its own section rather than bundled with bodyweight-specific
    fields. An unrecognised value falls back to the model default (lb).
    """

    unit_preference = forms.CharField(required=False)

    def save(self, user):
        User = get_user_model()
        user.unit_preference = (
            User.UnitPreference.KG
            if self.cleaned_data["unit_preference"] == "kg"
            else User.UnitPreference.LB
        )
        user.save(update_fields=["unit_preference"])


class LanguageForm(forms.Form):
    """Settings language-preference form.

    An empty value means "automatic" (browser-language detection via
    LocaleMiddleware). An unrecognised code falls back to automatic rather
    than blocking the whole submission.
    """

    language = forms.CharField(required=False)

    def save(self, user):
        code = self.cleaned_data["language"]
        user.language = code if code in dict(settings.LANGUAGES) else ""
        user.save(update_fields=["language"])


class TimezoneForm(forms.Form):
    """Settings timezone-preference form.

    An empty value means "automatic" (browser-detected pp_timezone cookie via
    UserTimezoneMiddleware). An unrecognised zone falls back to automatic
    rather than blocking the whole submission -- same contract as
    LanguageForm above.
    """

    timezone = forms.CharField(required=False)

    def save(self, user):
        value = self.cleaned_data["timezone"]
        user.timezone = value if is_valid_timezone(value) else ""
        user.save(update_fields=["timezone"])
