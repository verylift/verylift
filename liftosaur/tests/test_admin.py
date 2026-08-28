import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.tests.factories import UserFactory
from liftosaur.tests.factories import LiftHistoryFactory


@pytest.fixture
def staff_client(db):
    user = UserFactory(is_staff=True, is_superuser=True)
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestLiftHistoryAdmin:
    def test_changelist_does_not_n_plus_one_on_user(self, staff_client):
        for _ in range(5):
            LiftHistoryFactory()

        with CaptureQueriesContext(connection) as ctx:
            response = staff_client.get(
                reverse("admin:liftosaur_lifthistory_changelist")
            )

        assert response.status_code == 200
        user_lookup_queries = [
            q for q in ctx.captured_queries if "user" in q["sql"].lower()
        ]
        # One user row per row in the list would mean list_select_related
        # regressed back to N+1; a join keeps this well under that.
        assert len(user_lookup_queries) < 5

    def test_cannot_add_a_lift_history_row_via_the_admin(self, staff_client):
        response = staff_client.get(reverse("admin:liftosaur_lifthistory_add"))

        assert response.status_code == 403

    def test_change_view_renders_read_only_with_no_save_button(self, staff_client):
        entry = LiftHistoryFactory()

        response = staff_client.get(
            reverse("admin:liftosaur_lifthistory_change", args=[entry.pk])
        )

        assert response.status_code == 200
        assert b'name="_save"' not in response.content

    def test_changelist_search_resolves_the_user_relation(self, staff_client):
        # search_fields is deliberately restricted to the one indexed column
        # (see LiftHistoryAdmin's docstring); a related-lookup typo there only
        # raises when an operator types in the box, which no plain changelist
        # GET reaches.
        LiftHistoryFactory(user=UserFactory(username="searchable"))

        response = staff_client.get(
            reverse("admin:liftosaur_lifthistory_changelist"), {"q": "searchable"}
        )

        assert response.status_code == 200
        assert b"searchable" in response.content
