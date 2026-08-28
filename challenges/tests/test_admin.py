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
class TestGoalAdminChangelists:
    # Searched rather than merely opened: these admins reach across relations
    # (goal__participant__user__username, goal__name), and Django's system
    # checks do not validate a related-lookup path. A typo in one only
    # surfaces as a FieldError when an operator actually types in the search
    # box, so every changelist here is exercised *with* a query. This also
    # covers the plain render.
    @pytest.mark.parametrize(
        "route",
        [
            "admin:challenges_customgoal_changelist",
            "admin:challenges_customgoaltarget_changelist",
            "admin:challenges_reptargetgoal_changelist",
            "admin:challenges_reptargetgoaltarget_changelist",
        ],
    )
    def test_changelist_search_resolves_every_search_field(self, staff_client, route):
        CustomGoalTargetFactory()
        RepTargetGoalTargetFactory()

        response = staff_client.get(reverse(route), {"q": "squat"})

        assert response.status_code == 200


@pytest.mark.django_db
class TestRepTargetGoalAdmin:
    def test_change_view_shows_target_inline(self, staff_client):
        target = RepTargetGoalTargetFactory(lift="Pull-up")

        response = staff_client.get(
            reverse("admin:challenges_reptargetgoal_change", args=[target.goal_id])
        )

        assert response.status_code == 200
        assert b"Pull-up" in response.content

    def test_changelist_shows_a_goal_with_no_targets(self, staff_client):
        # The inline'd admins are the ones an operator opens on a goal whose
        # ladder is still empty; a target-less goal must not break the list.
        RepTargetGoalFactory()

        response = staff_client.get(
            reverse("admin:challenges_reptargetgoal_changelist")
        )

        assert response.status_code == 200
