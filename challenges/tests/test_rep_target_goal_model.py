"""Model-level tests for RepTargetGoal/RepTargetGoalTarget (issue #85)."""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from challenges.tests.factories import (
    RepTargetGoalFactory,
    RepTargetGoalTargetFactory,
)


@pytest.mark.django_db
class TestRepTargetGoalTargetConstraints:
    def test_target_reps_below_one_rejected(self):
        goal = RepTargetGoalFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            RepTargetGoalTargetFactory(goal=goal, target_reps=0)

    def test_target_reps_above_cap_rejected(self):
        goal = RepTargetGoalFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            RepTargetGoalTargetFactory(goal=goal, target_reps=1000)

    def test_unique_lift_per_goal_enforced(self):
        goal = RepTargetGoalFactory()
        RepTargetGoalTargetFactory(goal=goal, lift="Push Up")
        with pytest.raises(IntegrityError), transaction.atomic():
            RepTargetGoalTargetFactory(goal=goal, lift="Push Up")

    def test_str_includes_lift_reps_and_weight(self):
        target = RepTargetGoalTargetFactory(
            lift="Push Up", target_reps=20, target_weight=Decimal("0.00")
        )
        assert "Push Up" in str(target)
        assert "20" in str(target)
