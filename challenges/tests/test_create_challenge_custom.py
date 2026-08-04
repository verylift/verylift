"""Model/form-level guards for challenge creation (TASK-134).

View-level coverage of challenge creation (lift selection, presets,
persisted ChallengeLift rows) lives in test_create_challenge_view.py — every
challenge is CUSTOM-sourced (TASK-247/TASK-248), so that is the primary place
those behaviors are exercised.
"""

import pytest

from challenges.forms import CreateChallengeLiftsForm
from challenges.lift_presets import CLASSIC_LIFT_NAMES
from liftosaur.models import Lift

pytestmark = pytest.mark.django_db


class TestClassicsPreset:
    def test_unbound_form_pre_checks_exactly_the_classics(self):
        form = CreateChallengeLiftsForm()
        expected = set(
            Lift.objects.filter(name__in=CLASSIC_LIFT_NAMES).values_list(
                "pk", flat=True
            )
        )
        assert len(expected) == 14
        assert set(form.fields["lifts"].initial) == expected

    def test_bound_form_does_not_force_classics_over_posted_data(self):
        bench = Lift.objects.get(name="Bench Press")
        form = CreateChallengeLiftsForm(data={"lifts": [bench.pk]})
        assert form.is_valid()
        # Bound form keeps the POSTed selection; the Classics initial is not
        # applied on top of it.
        assert set(form.cleaned_data["lifts"].values_list("pk", flat=True)) == {
            bench.pk
        }
        assert not form.fields["lifts"].initial
