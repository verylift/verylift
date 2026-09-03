"""Release Lift and LiftHistory from the liftosaur app, in state only (TASK-347).

The matching half of core.0007, which adopts both models. Wrapped in
SeparateDatabaseAndState with no database_operations so this drops nothing:
the tables ("liftosaur_lift", "liftosaur_lifthistory") keep their names and
their rows, and only Django's idea of which app owns them changes.

The dependency on core.0007 is what makes the pair safe. Applied in that
order, the models are defined in core before liftosaur stops defining them.
Reversed, they would be defined in neither app for the duration of one
migration -- or, on a fresh database built from scratch, in both.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("liftosaur", "0010_remove_lift_is_liftosaur_builtin"),
        ("core", "0007_lift_lifthistory"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.DeleteModel(name="Lift"),
                migrations.DeleteModel(name="LiftHistory"),
            ],
        ),
    ]
