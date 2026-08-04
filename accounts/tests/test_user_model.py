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
    def test_user_has_uuid_pk(self):
        user = UserFactory()
        assert user.pk is not None
        assert len(str(user.pk)) == 36  # UUID format

    def test_str_returns_display_name(self):
        user = UserFactory(display_name="Alice")
        assert str(user) == "Alice"

    def test_str_falls_back_to_username(self):
        user = UserFactory(display_name="")
        assert str(user) == user.username

    def test_user_is_active_by_default(self):
        user = UserFactory()
        assert user.is_active is True

    def test_deactivated_at_is_null_by_default(self):
        user = UserFactory()
        assert user.deactivated_at is None

    def test_timezone_defaults_to_automatic(self):
        user = UserFactory()
        assert user.timezone == ""

    def test_liftosaur_api_key_is_null_by_default(self):
        user = UserFactory()
        assert user.liftosaur_api_key is None

    def test_liftosaur_api_key_can_be_set_and_cleared(self):
        user = UserFactory(liftosaur_api_key="abc123")
        assert user.liftosaur_api_key == "abc123"
        user.liftosaur_api_key = None
        user.save(update_fields=["liftosaur_api_key"])
        user.refresh_from_db()
        assert user.liftosaur_api_key is None


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
