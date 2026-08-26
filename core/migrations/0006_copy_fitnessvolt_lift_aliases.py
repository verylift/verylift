"""Copy every row out of the legacy fitnessvolt.FitnessVoltLiftAlias table
into the unified core.LiftAlias table (source="fitnessvolt").

Mirrors core.0004_copy_legacy_lift_aliases -- fitnessvolt.FitnessVoltLiftAlias
was missed by that earlier consolidation pass even though its docstring said
outright that it "[m]irrors liftosaur.models.LiftAlias exactly" (TASK-89), the
same duplication the other four tables were merged out of. Must run after
core.0005 (which adds the "fitnessvolt" choice to LiftAlias.source) and after
fitnessvolt's initial migration, so both historical models exist. The legacy
table itself is dropped separately, by fitnessvolt.0002_delete_fitnessvoltliftalias,
which depends on this migration -- so this migration is the only place the
old and new tables coexist, and the only place data can be lost if something
goes wrong.

Schema difference from the other four legacy tables: FitnessVoltLiftAlias's
raw-name column is called ``from_slug`` (unique on its own), not
``from_name`` scoped by a source discriminator -- it maps straight onto
core.LiftAlias's ``from_name`` under source="fitnessvolt".

The reverse migration copies rows back out of core.LiftAlias into
fitnessvolt.FitnessVoltLiftAlias, so this consolidation can be rolled back
without losing data either way.
"""

from django.db import migrations

SOURCE = "fitnessvolt"


def copy_into_unified_table(apps, schema_editor):
    LiftAlias = apps.get_model("core", "LiftAlias")
    FitnessVoltLiftAlias = apps.get_model("fitnessvolt", "FitnessVoltLiftAlias")
    rows = [
        LiftAlias(source=SOURCE, from_name=from_slug, to_name=to_name)
        for from_slug, to_name in FitnessVoltLiftAlias.objects.values_list(
            "from_slug", "to_name"
        )
    ]
    LiftAlias.objects.bulk_create(rows)


def copy_back_into_legacy_table(apps, schema_editor):
    """Reverse of ``copy_into_unified_table``: restores FitnessVoltLiftAlias's
    rows, then deletes them from core.LiftAlias -- leaving the schema exactly
    as it was before this migration ever ran forward, rather than leaving a
    duplicate copy sitting in core.LiftAlias that a later re-forward would
    collide with on the (source, from_name) uniqueness constraint.
    """
    LiftAlias = apps.get_model("core", "LiftAlias")
    FitnessVoltLiftAlias = apps.get_model("fitnessvolt", "FitnessVoltLiftAlias")
    source_aliases = LiftAlias.objects.filter(source=SOURCE)
    rows = [
        FitnessVoltLiftAlias(from_slug=from_name, to_name=to_name)
        for from_name, to_name in source_aliases.values_list("from_name", "to_name")
    ]
    FitnessVoltLiftAlias.objects.bulk_create(rows)
    source_aliases.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_alter_liftalias_source"),
        ("fitnessvolt", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(copy_into_unified_table, copy_back_into_legacy_table),
    ]
