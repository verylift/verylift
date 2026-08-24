"""Model/form-level guards for challenge creation (TASK-134).

View-level coverage of challenge creation (lift selection, presets,
persisted ChallengeLift rows) lives in test_create_challenge_view.py — every
challenge is CUSTOM-sourced (TASK-247/TASK-248), so that is the primary place
those behaviors are exercised.
"""

import pytest

from challenges.forms import CreateChallengeLiftsForm
from challenges.models import Challenge
from liftosaur.models import Lift

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

    def test_default_tab_follows_mode(self):
        assert CreateChallengeLiftsForm().default_lift_tab == "popular"
        assert (
            CreateChallengeLiftsForm(mode=Challenge.Mode.REP_TARGET).default_lift_tab
            == "calisthenics"
        )
