"""Tests for the User admin registration (TASK-67, TASK-124)."""

from unittest.mock import patch

import pytest
from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.test import Client, RequestFactory
from django.urls import reverse

from accounts.tests.factories import UserFactory

User = get_user_model()


@pytest.fixture
def staff_client(db):
    user = UserFactory(is_staff=True, is_superuser=True)
    c = Client()
    c.force_login(user)
    return c


def test_staff_can_access_user_changelist(staff_client):
    response = staff_client.get(reverse("admin:accounts_user_changelist"))
    assert response.status_code == 200


def test_staff_can_access_user_add(staff_client):
    response = staff_client.get(reverse("admin:accounts_user_add"))
    assert response.status_code == 200


@pytest.mark.django_db
class TestPasswordHandling:
    def test_change_form_password_field_is_readonly_hash(self):
        staff = UserFactory(is_staff=True, is_superuser=True)
        user = UserFactory()
        request = RequestFactory().get("/")
        request.user = staff
        model_admin = site._registry[User]
        form_class = model_admin.get_form(request=request, obj=user)
        password_field = form_class.base_fields["password"]
        assert isinstance(password_field, ReadOnlyPasswordHashField)

    def test_change_form_links_to_password_change(self, staff_client):
        user = UserFactory()
        response = staff_client.get(
            reverse("admin:accounts_user_change", args=[user.pk])
        )
        assert response.status_code == 200
        # The change form links to the dedicated hashed-password change view
        # (rendered as a relative "../password/" link) rather than exposing an
        # editable password field.
        assert b"../password/" in response.content
        assert reverse("admin:auth_user_password_change", args=[user.pk])

    def test_editing_user_does_not_corrupt_password(self, staff_client):
        user = UserFactory()
        user.set_password("original-pass")
        user.save()
        original_hash = user.password

        response = staff_client.post(
            reverse("admin:accounts_user_change", args=[user.pk]),
            {
                "username": user.username,
                "email": user.email,
                "display_name": "Renamed",
                "is_active": "on",
                "date_joined_0": "2026-01-01",
                "date_joined_1": "00:00:00",
            },
            follow=True,
        )
        assert response.status_code == 200

        user.refresh_from_db()
        assert user.display_name == "Renamed"
        assert user.password == original_hash
        assert user.check_password("original-pass")

    def test_admin_can_set_password_via_change_password_flow(self, staff_client):
        user = UserFactory()
        user.set_password("old-pass")
        user.save()

        response = staff_client.post(
            reverse("admin:auth_user_password_change", args=[user.pk]),
            {"password1": "brand-new-pass-123", "password2": "brand-new-pass-123"},
            follow=True,
        )
        assert response.status_code == 200

        user.refresh_from_db()
        assert user.check_password("brand-new-pass-123")
        assert not user.check_password("old-pass")

    def test_add_form_creates_user_with_hashed_password(self, staff_client):
        response = staff_client.post(
            reverse("admin:accounts_user_add"),
            {
                "username": "newadminuser",
                "password1": "creation-pass-456",
                "password2": "creation-pass-456",
                "email": "newadminuser@example.com",
                "display_name": "New Admin User",
                "is_active": "on",
            },
            follow=True,
        )
        assert response.status_code == 200

        created = User.objects.get(username="newadminuser")
        assert created.password != "creation-pass-456"
        assert created.check_password("creation-pass-456")


@pytest.mark.django_db
class TestBackfillLiftHistoryAction:
    def _run_action(self, staff_client, user_ids):
        return staff_client.post(
            reverse("admin:accounts_user_changelist"),
            {
                "action": "backfill_lift_history",
                "_selected_action": user_ids,
            },
            follow=True,
        )

    def test_action_forces_backfill_for_each_selected_user(self, staff_client):
        users = [UserFactory(), UserFactory()]
        with patch("accounts.admin.sync_user_lifts") as mock_backfill:
            response = self._run_action(staff_client, [u.pk for u in users])

        assert response.status_code == 200
        assert mock_backfill.call_count == 2
        for call in mock_backfill.call_args_list:
            assert call.kwargs == {"force": True}
        assert {call.args[0].pk for call in mock_backfill.call_args_list} == {
            u.pk for u in users
        }
        messages = [str(m) for m in response.context["messages"]]
        assert any("2 user(s)" in m for m in messages)

    def test_action_reports_per_user_failure(self, staff_client):
        good = UserFactory()
        bad = UserFactory()

        def side_effect(user, force):
            if user.pk == bad.pk:
                raise RuntimeError("boom")

        with patch("accounts.admin.sync_user_lifts", side_effect=side_effect):
            response = self._run_action(staff_client, [good.pk, bad.pk])

        messages = [str(m) for m in response.context["messages"]]
        assert any(bad.username in m and "failed" in m.lower() for m in messages)
        assert any("1 user(s)" in m for m in messages)


@pytest.mark.django_db
class TestLiftosaurKeyIsNotExposed:
    """The change form must not render the key (TASK-285, AC#4)."""

    KEY = "liftosaur-admin-secret-xyz789"

    def test_change_form_shows_a_mask_instead_of_the_key(self, staff_client):
        user = UserFactory(liftosaur_api_key=self.KEY)

        response = staff_client.get(
            reverse("admin:accounts_user_change", args=[user.pk])
        )

        assert response.status_code == 200
        assert self.KEY.encode() not in response.content
        assert "••••••••xyz789".encode() in response.content

    def test_masked_display_falls_back_to_a_dash_when_no_key_is_set(self):
        user = UserFactory(liftosaur_api_key=None)

        assert site._registry[User].liftosaur_api_key_masked(user) == "—"

    def test_change_form_renders_for_a_user_without_a_key(self, staff_client):
        user = UserFactory(liftosaur_api_key=None)

        response = staff_client.get(
            reverse("admin:accounts_user_change", args=[user.pk])
        )

        assert response.status_code == 200
        assert b"Liftosaur API key" in response.content

    def test_key_is_not_a_form_field(self, staff_client):
        user = UserFactory(liftosaur_api_key=self.KEY)
        request = RequestFactory().get("/")
        request.user = user
        form_class = site._registry[User].get_form(request=request, obj=user)

        assert "liftosaur_api_key" not in form_class.base_fields

    def test_saving_the_change_form_leaves_the_key_intact(self, staff_client):
        user = UserFactory(liftosaur_api_key=self.KEY)

        response = staff_client.post(
            reverse("admin:accounts_user_change", args=[user.pk]),
            {
                "username": user.username,
                "email": user.email,
                "display_name": "Renamed",
                "is_active": "on",
                "date_joined_0": "2026-01-01",
                "date_joined_1": "00:00:00",
            },
            follow=True,
        )
        assert response.status_code == 200

        user.refresh_from_db()
        assert user.display_name == "Renamed"
        assert user.liftosaur_api_key == self.KEY
