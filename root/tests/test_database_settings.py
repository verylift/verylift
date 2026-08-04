"""DATABASE_URL fallback checks (TASK-231).

Verifies that root.settings falls back to SQLite when DATABASE_URL is
unset (for hosts that can't run Postgres), and still uses Postgres whenever
DATABASE_URL is configured.
"""

import importlib

import environ

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
