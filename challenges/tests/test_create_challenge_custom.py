"""Model/form-level guards for challenge creation (TASK-134).

View-level coverage of challenge creation (lift selection, presets,
persisted ChallengeLift rows) lives in test_create_challenge_view.py — every
challenge is CUSTOM-sourced (TASK-247/TASK-248), so that is the primary place
those behaviors are exercised.
"""

import pytest

from challenges.forms import CreateChallengeLiftsForm
from core.models import Lift

pytestmark = pytest.mark.django_db


class TestLiftTabMembership:
    def test_unbound_form_pre_checks_nothing(self):
        """No silent default preset (issue #85 follow-up): the picker's
        "Popular"/"Calisthenics" tabs are select-all shortcuts an owner
        chooses, not a pre-applied default they might not notice."""
        form = CreateChallengeLiftsForm()
        assert not form.fields["lifts"].initial

    def test_bound_form_keeps_posted_selection(self):
        bench = Lift.objects.get(name="Bench Press")
        form = CreateChallengeLiftsForm(data={"lifts": [bench.pk]})
        assert form.is_valid()
        assert set(form.cleaned_data["lifts"].values_list("pk", flat=True)) == {
            bench.pk
        }

    def test_tab_presets_are_subsets_of_the_full_catalogue(self):
        """The tabs filter one rendered list rather than adding lifts of their
        own, so a name in either preset must exist in the queryset backing it
        or its rows would silently go missing from that tab."""
        form = CreateChallengeLiftsForm()
        catalogue = set(form.fields["lifts"].queryset.values_list("name", flat=True))
        assert form.classic_lift_names <= catalogue
        assert form.calisthenics_lift_names <= catalogue
