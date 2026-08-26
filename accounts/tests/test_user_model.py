from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image

from accounts.tests.factories import UserFactory

User = get_user_model()


def _tiny_png_content():
    buffer = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(buffer, format="PNG")
    return ContentFile(buffer.getvalue(), name="avatar.png")


@pytest.mark.django_db
class TestUserManager:
    def test_create_user_returns_user_with_unusable_password(self):
        user = User.objects.create_user(username="testmgr", email="mgr@example.com")
        assert user.pk is not None
        assert not user.has_usable_password()

    def test_create_user_defaults_email_to_empty_string(self):
        user = User.objects.create_user(username="noemail")
        assert user.email == ""

    def test_create_superuser_sets_is_staff_and_is_superuser(self):
        superuser = User.objects.create_superuser(username="superadmin-test")
        assert superuser.is_staff is True
        assert superuser.is_superuser is True


@pytest.mark.django_db
class TestUserModel:
    def test_str_returns_display_name(self):
        user = UserFactory(display_name="Alice")
        assert str(user) == "Alice"

    def test_str_falls_back_to_username(self):
        user = UserFactory(display_name="")
        assert str(user) == user.username


class TestHasConnectedTracker:
    """Pure attribute check -- no factory/db needed, unsaved instances only."""

    def test_false_with_no_credentials(self):
        assert User().has_connected_tracker is False

    def test_true_with_liftosaur_key_only(self):
        assert User(liftosaur_api_key="key").has_connected_tracker is True

    def test_true_with_hevy_key_only(self):
        assert User(hevy_api_key="key").has_connected_tracker is True

    def test_true_with_both_wger_fields(self):
        user = User(wger_instance_url="https://example.com", wger_api_token="tok")
        assert user.has_connected_tracker is True

    def test_false_with_only_wger_instance_url(self):
        """Wger needs both fields together -- a URL alone can't authenticate."""
        assert (
            User(wger_instance_url="https://example.com").has_connected_tracker is False
        )

    def test_false_with_only_wger_api_token(self):
        assert User(wger_api_token="tok").has_connected_tracker is False


@pytest.mark.django_db
class TestAvatarFileCleanupOnDelete:
    @pytest.fixture(autouse=True)
    def _media_root(self, settings, tmp_path):
        settings.MEDIA_ROOT = tmp_path

    def test_deleting_user_removes_avatar_file(self):
        user = UserFactory()
        user.avatar.save("avatar.png", _tiny_png_content(), save=True)
        avatar_path = user.avatar.name
        assert default_storage.exists(avatar_path)

        user.delete()

        assert not default_storage.exists(avatar_path)

    def test_deleting_user_without_avatar_does_not_error(self):
        user = UserFactory()
        assert not user.avatar

        user.delete()
