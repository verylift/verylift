"""Coverage for the authenticated gate in front of MEDIA_ROOT (TASK-277).

Nothing else in the suite fetches a media file through the real URLconf --
accounts/tests/test_settings.py calls serve_static directly with a
RequestFactory request (see the comment there) -- so these are the tests that
prove the wiring in root/urls.py behaves.
"""

from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.test import Client
from django.urls import resolve

from accounts.tests.factories import UserFactory

# serve_static resolves the content type from the file *name* via mimetypes, so
# real AVIF bytes are unnecessary here; the payload only has to be recognisable.
_AVATAR_BYTES = b"avatar-file-contents"
_MISSING_URL = "/media/avatars/definitely-not-here.avif"


def _body(response):
    if response.streaming:
        return b"".join(response.streaming_content)
    return response.content


@pytest.mark.django_db
class TestProtectedMediaView:
    @pytest.fixture(autouse=True)
    def _media_root(self, settings):
        # root/urls.py evaluates settings.MEDIA_ROOT once, when the
        # module is imported, to bind the view's document_root kwarg -- the same
        # import-time binding accounts/tests/test_settings.py documents. So
        # pointing MEDIA_ROOT at a tmp_path here would make default_storage
        # write somewhere the wired-up view never looks (which reads as a
        # spurious 404, and only in some test orderings). Line MEDIA_ROOT up
        # with the root the URL actually serves from instead; the file each test
        # writes there is cleaned up by the user fixture below.
        served_root = Path(resolve("/media/probe").kwargs["document_root"])
        served_root.mkdir(parents=True, exist_ok=True)
        settings.MEDIA_ROOT = served_root

    @pytest.fixture
    def user(self):
        user = UserFactory()
        user.avatar.save("task-277-avatar.avif", ContentFile(_AVATAR_BYTES), save=True)
        yield user
        user.avatar.delete(save=False)

    def test_anonymous_request_for_an_existing_avatar_is_forbidden(self, user):
        response = Client().get(user.avatar.url)

        assert response.status_code == 403
        assert _AVATAR_BYTES not in response.content

    def test_anonymous_request_for_a_missing_path_is_forbidden_too(self, user):
        # Same 403 as the existing file above: the gate runs before any
        # filesystem access, so a logged-out client cannot use the status code
        # to learn whether a given avatar path exists.
        response = Client().get(_MISSING_URL)

        assert response.status_code == 403

    def test_authenticated_request_serves_the_file(self, client, user):
        client.force_login(user)

        response = client.get(user.avatar.url)

        assert response.status_code == 200
        assert _body(response) == _AVATAR_BYTES

    def test_authenticated_request_keeps_the_avif_content_type(self, client, user):
        client.force_login(user)

        response = client.get(user.avatar.url)

        assert response["Content-Type"] == "image/avif"

    def test_authenticated_request_for_a_missing_file_is_still_404(self, client, user):
        client.force_login(user)

        response = client.get(_MISSING_URL)

        assert response.status_code == 404

    def test_authenticated_response_is_not_shared_cacheable(self, client, user):
        client.force_login(user)

        response = client.get(user.avatar.url)

        assert "private" in response["Cache-Control"]

    def test_conditional_request_still_returns_304(self, client, user):
        # serve_static's Last-Modified handling must survive the wrapper --
        # this is what keeps repeat avatar views cheap.
        client.force_login(user)
        first = client.get(user.avatar.url)

        second = client.get(
            user.avatar.url, HTTP_IF_MODIFIED_SINCE=first["Last-Modified"]
        )

        assert second.status_code == 304
