"""DATABASE_URL fallback checks (TASK-231).

Verifies that root.settings falls back to SQLite when DATABASE_URL is
unset (for hosts that can't run Postgres), and still uses Postgres whenever
DATABASE_URL is configured, and that the SQLite path is put into WAL mode
(issue #16) without touching the Postgres path.
"""

import importlib

import environ
from django.db import ConnectionHandler

import root.settings as base_settings


class TestDatabaseUrlFallback:
    def test_falls_back_to_sqlite_when_unset(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        # settings.py calls environ.Env.read_env(BASE_DIR / ".env", overwrite=False)
        # at module level, which importlib.reload re-runs. On any machine with a
        # real .env (i.e. everyone who followed the README's `cp example.env .env`
        # setup), that call would see DATABASE_URL as "not already in os.environ"
        # -- monkeypatch.delenv just removed it -- and restore it straight from
        # the file, silently defeating this test. Block it so the fallback path
        # is exercised regardless of what a local .env happens to contain.
        monkeypatch.setattr(environ.Env, "read_env", lambda *args, **kwargs: None)
        reloaded = importlib.reload(base_settings)

        assert reloaded.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
        assert reloaded.DATABASES["default"]["NAME"] == str(
            reloaded.BASE_DIR / "db.sqlite3"
        )

    def test_uses_postgres_when_database_url_set(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL", "postgres://verylift:verylift@db:5432/verylift"
        )
        reloaded = importlib.reload(base_settings)

        assert (
            reloaded.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"
        )
        assert reloaded.DATABASES["default"]["NAME"] == "verylift"


class TestSqliteWalMode:
    """Issue #16 -- the SQLite default must run in WAL mode.

    Several gunicorn workers share one database file, and rollback-journal mode
    serializes writes behind a database-wide lock.
    """

    def _sqlite_settings(self, monkeypatch, wal=None):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        if wal is None:
            monkeypatch.delenv("SQLITE_WAL", raising=False)
        else:
            monkeypatch.setenv("SQLITE_WAL", wal)
        # See the comment in TestDatabaseUrlFallback -- a real .env on disk
        # would otherwise restore DATABASE_URL during the reload.
        monkeypatch.setattr(environ.Env, "read_env", lambda *args, **kwargs: None)
        return importlib.reload(base_settings).DATABASES["default"]

    def test_sqlite_default_configures_wal(self, monkeypatch):
        options = self._sqlite_settings(monkeypatch)["OPTIONS"]

        assert "PRAGMA journal_mode=WAL;" in options["init_command"]
        assert "PRAGMA busy_timeout=5000;" in options["init_command"]
        assert options["transaction_mode"] == "IMMEDIATE"

    def test_wal_takes_effect_on_a_real_file_database(
        self, monkeypatch, tmp_path, django_db_blocker
    ):
        """The pragmas are asserted through a real connection, not just as config.

        A file-backed database is the case that matters: journal_mode is a
        property of the file, and an in-memory database silently reports
        "memory" no matter what is asked for.
        """
        settings_dict = dict(self._sqlite_settings(monkeypatch))
        settings_dict["NAME"] = str(tmp_path / "db.sqlite3")

        connections = ConnectionHandler({"default": settings_dict})
        # This throwaway connection is outside pytest-django's managed test
        # database, so its access blocker has to be lifted explicitly.
        try:
            with django_db_blocker.unblock(), connections["default"].cursor() as cursor:
                cursor.execute("PRAGMA journal_mode;")
                journal_mode = cursor.fetchone()[0]
                cursor.execute("PRAGMA busy_timeout;")
                busy_timeout = cursor.fetchone()[0]
                cursor.execute("CREATE TABLE wal_probe (id integer primary key);")
                # WAL's sidecar files live beside the database while a
                # connection is open, so the directory has to be writable, not
                # just the file -- worth pinning for the volume-mounted path.
                # They are checkpointed away on a clean close, hence the check
                # here rather than after close_all().
                sidecars = sorted(p.name for p in tmp_path.glob("db.sqlite3-*"))
        finally:
            connections.close_all()

        assert journal_mode == "wal"
        assert busy_timeout == 5000
        assert sidecars == ["db.sqlite3-shm", "db.sqlite3-wal"]

    def test_sqlite_wal_false_returns_to_rollback_journal(self, monkeypatch):
        """Operators on storage without usable mmap need an escape hatch."""
        options = self._sqlite_settings(monkeypatch, wal="False")["OPTIONS"]

        assert "PRAGMA journal_mode=DELETE;" in options["init_command"]
        # NORMAL is only corruption-safe under WAL.
        assert "PRAGMA synchronous=FULL;" in options["init_command"]
        assert "PRAGMA busy_timeout=5000;" in options["init_command"]

    def test_disabled_wal_converts_an_existing_wal_file_back(
        self, monkeypatch, tmp_path, django_db_blocker
    ):
        """Journal mode lives in the file, so flipping the env var must undo it."""
        db_path = str(tmp_path / "db.sqlite3")

        for wal, expected in (("True", "wal"), ("False", "delete")):
            settings_dict = dict(self._sqlite_settings(monkeypatch, wal=wal))
            settings_dict["NAME"] = db_path
            connections = ConnectionHandler({"default": settings_dict})
            try:
                with (
                    django_db_blocker.unblock(),
                    connections["default"].cursor() as cursor,
                ):
                    cursor.execute("PRAGMA journal_mode;")
                    assert cursor.fetchone()[0] == expected
            finally:
                connections.close_all()

    def test_postgres_gets_no_sqlite_options(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL", "postgres://verylift:verylift@db:5432/verylift"
        )
        reloaded = importlib.reload(base_settings)

        assert "init_command" not in reloaded.DATABASES["default"].get("OPTIONS", {})
