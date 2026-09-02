"""Drop Lift.is_liftosaur_builtin, which never carried any information.

The column was true on all 139 fixture rows. That is not an accident: the
catalogue was derived from Liftosaur's own exercise list, so every row was
built-in by construction and the flag restated this table's own membership
criterion rather than distinguishing anything within it.

Nothing consumed it either. ``Lift.builtin_names()`` had one caller,
``liftosaur.services.liftosaur_builtin_lift_names()``, which had none; both
are removed alongside the column. The feature it was groundwork for -- warning
that a lift needs a custom exercise provisioned in the user's Liftosaur
account -- was never built, and could not have been served by this data
anyway.

Deliberately dropped rather than carried forward: once the lift register
becomes tracker-agnostic, lifts can enter from another tracker's catalogue
that Liftosaur does not ship, at which point the flag turns meaningful and
wrong -- new rows defaulting false while the existing 139 stay blanket-true
with none of them verified. If per-lift tracker facts are ever genuinely
needed, they belong in a (source, lift) join table alongside core.LiftAlias,
not as a column here.

Purely additive to drop: no data depends on it and no code reads it.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("liftosaur", "0009_restamp_strong_source_value"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="lift",
            name="is_liftosaur_builtin",
        ),
    ]
