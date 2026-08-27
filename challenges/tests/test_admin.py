import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.tests.factories import (
    CustomGoalTargetFactory,
    RepTargetGoalFactory,
    RepTargetGoalTargetFactory,
)


@pytest.fixture
def staff_client(db):
    user = UserFactory(is_staff=True, is_superuser=True)
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestRepTargetGoalAdmin:
    def test_changelist_renders(self, staff_client):
        RepTargetGoalFactory()

        response = staff_client.get(
            reverse("admin:challenges_reptargetgoal_changelist")
        )

        assert response.status_code == 200

    def test_change_view_shows_target_inline(self, staff_client):
        target = RepTargetGoalTargetFactory(lift="Pull-up")

        response = staff_client.get(
            reverse("admin:challenges_reptargetgoal_change", args=[target.goal_id])
        )

        assert response.status_code == 200
        assert b"Pull-up" in response.content


@pytest.mark.django_db
class TestRepTargetGoalTargetAdmin:
    def test_changelist_renders(self, staff_client):
        RepTargetGoalTargetFactory()

        response = staff_client.get(
            reverse("admin:challenges_reptargetgoaltarget_changelist")
        )

        assert response.status_code == 200


@pytest.mark.django_db
class TestCustomGoalTargetAdmin:
    def test_changelist_renders(self, staff_client):
        CustomGoalTargetFactory()

        response = staff_client.get(
            reverse("admin:challenges_customgoaltarget_changelist")
        )

        assert response.status_code == 200
