"""Exercises the 0004 legacy-alias-consolidation data migration against real
Postgres schema state, in both directions.

Unlike policies.tests.test_migrations (which calls a RunPython function
directly against the final, already-migrated schema because the tables it
touches still exist there), this migration's legacy tables
(liftosaur.LiftAlias, workout_imports.HevyLiftAlias/StrongLiftAlias,
wger.WgerLiftAlias) are DROPPED by later migrations in the normal migration
graph. Calling the RunPython function directly against the final schema would
fail with "relation does not exist" for tables that are gone by then. This
uses Django's MigrationExecutor to actually walk the schema back to just
before 0004, exercise the real forward/backward RunPython functions against
historical (not current) models, and walk it back forward again -- the same
migrate-down-then-up cycle the manual verification for this refactor used,
automated.

Schema-mutating and slow (each direction change runs real DDL), so this stays
its own small, focused module rather than folding into the regular
core/liftosaur/workout_imports/wger test suites.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

_BEFORE = [
    ("core", "0003_liftalias"),
    ("liftosaur", "0005_alter_lifthistory_source"),
    ("workout_imports", "0002_strongliftalias"),
    ("wger", "0001_wger_integration"),
]
_AFTER = [
    ("core", "0004_copy_legacy_lift_aliases"),
    ("liftosaur", "0006_delete_liftalias"),
    ("workout_imports", "0003_delete_hevyliftalias_delete_strongliftalias"),
    ("wger", "0002_delete_wgerliftalias"),
]


_ALL_TABLES = (
    "core_liftalias",
    "liftosaur_liftalias",
    "hevy_liftalias",
    "strong_liftalias",
    "wger_liftalias",
)


def _migrate(targets):
    # A fresh MigrationExecutor every call, not one reused across multiple
    # migrate() calls in the same test: its loader snapshots applied-migration
    # state at construction time, which goes stale the moment a prior
    # migrate() call changes it -- reusing one executor across a down-then-up
    # cycle silently computes the wrong plan (e.g. re-running the forward
    # RunPython when it should be unapplying).
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor


def _clear_all_alias_tables():
    # core_liftalias exists across the whole _BEFORE<->_AFTER range (only
    # created/dropped by 0003, which every test here stays above), so
    # transaction=True (required for MigrationExecutor's real DDL) means its
    # rows survive from one test to the next unless explicitly cleared.
    # liftosaur_liftalias also needs clearing even freshly after a down
    # migration: core.0004's own reverse function restores the real
    # fixture-seeded aliases django_db_setup wrote into it, which would
    # collide with (and confuse assertions about) this test's own rows.
    with connection.cursor() as cursor:
        for table in _ALL_TABLES:
            cursor.execute(f"TRUNCATE TABLE {table}")  # noqa: S608 -- fixed allowlist


@pytest.fixture
def _before_state(request):
    """Migrate the schema down to just before 0004 and return its ``apps``.

    Every test starts from this same clean slate; each test then drives
    further ``_migrate()`` calls itself.
    """
    executor = _migrate(_BEFORE)
    _clear_all_alias_tables()

    def _restore():
        # Bring every app back to its latest migration (django_db_setup's
        # normal end state) so later tests see the usual fully-migrated
        # schema regardless of which direction this test left it in.
        call_command("migrate", verbosity=0)

    request.addfinalizer(_restore)
    return executor.loader.project_state(_BEFORE).apps


def _seed_legacy_rows(apps):
    # Names deliberately don't collide with the real fixture-seeded aliases
    # (django_db_setup's session-scoped seed_liftosaur_lifts call, whose 9
    # rows core.0004's own reverse function restores into
    # liftosaur_liftalias as part of getting the schema down to _BEFORE) --
    # a from_name this test also uses would hit the real unique constraint
    # on the legacy table before this migration is even exercised.
    apps.get_model("liftosaur", "LiftAlias").objects.using(connection.alias).create(
        from_name="Migration Test Liftosaur Squat", to_name="Back Squat"
    )
    apps.get_model("workout_imports", "HevyLiftAlias").objects.using(
        connection.alias
    ).create(from_name="Migration Test Hevy Squat (Barbell)", to_name="Back Squat")
    apps.get_model("workout_imports", "StrongLiftAlias").objects.using(
        connection.alias
    ).create(
        from_name="Migration Test Strong Pendlay Row (Barbell)",
        to_name="Pendlay Row",
    )
    apps.get_model("wger", "WgerLiftAlias").objects.using(connection.alias).create(
        from_name="Migration Test Wger Barbell Squat", to_name="Back Squat"
    )


class TestCopyLegacyLiftAliasesMigration:
    def test_forward_copies_every_legacy_table_into_the_unified_table(
        self, _before_state
    ):
        _seed_legacy_rows(_before_state)

        executor = _migrate(_AFTER)

        apps = executor.loader.project_state(_AFTER).apps
        LiftAlias = apps.get_model("core", "LiftAlias")
        rows = set(
            LiftAlias.objects.using(connection.alias).values_list(
                "source", "from_name", "to_name"
            )
        )
        assert rows == {
            ("liftosaur", "Migration Test Liftosaur Squat", "Back Squat"),
            ("hevy", "Migration Test Hevy Squat (Barbell)", "Back Squat"),
            (
                "strong",
                "Migration Test Strong Pendlay Row (Barbell)",
                "Pendlay Row",
            ),
            ("wger", "Migration Test Wger Barbell Squat", "Back Squat"),
        }

    def test_backward_restores_rows_into_the_legacy_tables_and_clears_the_unified_one(
        self, _before_state
    ):
        _seed_legacy_rows(_before_state)
        _migrate(_AFTER)

        executor = _migrate(_BEFORE)

        apps = executor.loader.project_state(_BEFORE).apps
        assert list(
            apps.get_model("liftosaur", "LiftAlias")
            .objects.using(connection.alias)
            .values_list("from_name", "to_name")
        ) == [("Migration Test Liftosaur Squat", "Back Squat")]
        assert list(
            apps.get_model("workout_imports", "HevyLiftAlias")
            .objects.using(connection.alias)
            .values_list("from_name", "to_name")
        ) == [("Migration Test Hevy Squat (Barbell)", "Back Squat")]
        assert list(
            apps.get_model("workout_imports", "StrongLiftAlias")
            .objects.using(connection.alias)
            .values_list("from_name", "to_name")
        ) == [("Migration Test Strong Pendlay Row (Barbell)", "Pendlay Row")]
        assert list(
            apps.get_model("wger", "WgerLiftAlias")
            .objects.using(connection.alias)
            .values_list("from_name", "to_name")
        ) == [("Migration Test Wger Barbell Squat", "Back Squat")]

    def test_backward_leaves_no_duplicate_rows_in_the_unified_table(
        self, _before_state
    ):
        # Regression: an earlier version of the reverse function restored the
        # legacy rows but never removed them from core.LiftAlias, so
        # re-running the forward function afterwards hit the (source,
        # from_name) uniqueness constraint on the duplicate.
        _seed_legacy_rows(_before_state)
        _migrate(_AFTER)

        _migrate(_BEFORE)
        # Re-forward must not raise IntegrityError.
        executor = _migrate(_AFTER)

        apps = executor.loader.project_state(_AFTER).apps
        LiftAlias = apps.get_model("core", "LiftAlias")
        assert LiftAlias.objects.using(connection.alias).count() == 4


_BEFORE_FV = [
    ("core", "0005_alter_liftalias_source"),
    ("fitnessvolt", "0001_initial"),
]
_AFTER_FV = [
    ("core", "0006_copy_fitnessvolt_lift_aliases"),
    ("fitnessvolt", "0002_delete_fitnessvoltliftalias"),
]


@pytest.fixture
def _before_fv_state(request):
    """Migrate the schema down to just before core.0006 and return its ``apps``.

    Unlike core_liftalias in the four-source consolidation above, this table
    holds rows for every source (liftosaur/hevy/strong/wger/fitnessvolt) at
    every point in this test module's migration range, so it must not be
    blanket-truncated here -- core.0006's own reverse function already moves
    every source="fitnessvolt" row out into fitnessvolt_liftalias as part of
    walking the schema back to this state, leaving zero fitnessvolt rows in
    core_liftalias and the real 16 fixture-seeded rows in
    fitnessvolt_liftalias. Only fitnessvolt_liftalias needs clearing, so this
    test's own dummy rows don't collide with the real ones on the from_slug
    uniqueness constraint.
    """
    executor = _migrate(_BEFORE_FV)
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE TABLE fitnessvolt_liftalias")

    def _restore():
        call_command("migrate", verbosity=0)

    request.addfinalizer(_restore)
    return executor.loader.project_state(_BEFORE_FV).apps


def _seed_legacy_fitnessvolt_rows(apps):
    FitnessVoltLiftAlias = apps.get_model("fitnessvolt", "FitnessVoltLiftAlias")
    FitnessVoltLiftAlias.objects.using(connection.alias).bulk_create(
        [
            FitnessVoltLiftAlias(
                from_slug="migration-test-squat", to_name="Back Squat"
            ),
            FitnessVoltLiftAlias(
                from_slug="migration-test-bench", to_name="Bench Press"
            ),
            FitnessVoltLiftAlias(
                from_slug="migration-test-deadlift", to_name="Deadlift"
            ),
        ]
    )


class TestCopyFitnessVoltLiftAliasesMigration:
    """Exercises core.0006_copy_fitnessvolt_lift_aliases against real
    Postgres schema state, in both directions -- fitnessvolt's own version of
    TestCopyLegacyLiftAliasesMigration above, for the fifth alias table that
    the original four-source consolidation missed (TASK-89 mirrors
    liftosaur.models.LiftAlias, same duplication pattern).
    """

    def test_forward_copies_fitnessvoltliftalias_into_the_unified_table(
        self, _before_fv_state
    ):
        _seed_legacy_fitnessvolt_rows(_before_fv_state)

        executor = _migrate(_AFTER_FV)

        apps = executor.loader.project_state(_AFTER_FV).apps
        LiftAlias = apps.get_model("core", "LiftAlias")
        rows = set(
            LiftAlias.objects.using(connection.alias)
            .filter(source="fitnessvolt")
            .values_list("source", "from_name", "to_name")
        )
        assert rows == {
            ("fitnessvolt", "migration-test-squat", "Back Squat"),
            ("fitnessvolt", "migration-test-bench", "Bench Press"),
            ("fitnessvolt", "migration-test-deadlift", "Deadlift"),
        }

    def test_backward_restores_rows_and_clears_the_unified_table(
        self, _before_fv_state
    ):
        _seed_legacy_fitnessvolt_rows(_before_fv_state)
        _migrate(_AFTER_FV)

        executor = _migrate(_BEFORE_FV)

        apps = executor.loader.project_state(_BEFORE_FV).apps
        FitnessVoltLiftAlias = apps.get_model("fitnessvolt", "FitnessVoltLiftAlias")
        assert set(
            FitnessVoltLiftAlias.objects.using(connection.alias).values_list(
                "from_slug", "to_name"
            )
        ) == {
            ("migration-test-squat", "Back Squat"),
            ("migration-test-bench", "Bench Press"),
            ("migration-test-deadlift", "Deadlift"),
        }
        LiftAlias = apps.get_model("core", "LiftAlias")
        assert (
            not LiftAlias.objects.using(connection.alias)
            .filter(source="fitnessvolt")
            .exists()
        )

    def test_backward_leaves_no_duplicate_rows_in_the_unified_table(
        self, _before_fv_state
    ):
        # Regression: mirrors the equivalent guard above -- the reverse
        # function must actually remove the copied rows from core.LiftAlias,
        # or a re-forward hits the (source, from_name) uniqueness constraint.
        _seed_legacy_fitnessvolt_rows(_before_fv_state)
        _migrate(_AFTER_FV)

        _migrate(_BEFORE_FV)
        executor = _migrate(_AFTER_FV)

        apps = executor.loader.project_state(_AFTER_FV).apps
        LiftAlias = apps.get_model("core", "LiftAlias")
        assert (
            LiftAlias.objects.using(connection.alias)
            .filter(source="fitnessvolt")
            .count()
            == 3
        )
