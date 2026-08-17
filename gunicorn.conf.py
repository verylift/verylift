"""Gunicorn configuration for very lift production deployment."""

import multiprocessing
import os

import structlog

bind = "0.0.0.0:8000"

# Worker count formula: (2 x CPU cores) + 1. multiprocessing.cpu_count() reports
# the cores visible to the container; if it cannot be determined we assume a
# 2-core VPS, giving the documented default of 5 workers.
try:
    _cores = multiprocessing.cpu_count()
except NotImplementedError:
    _cores = 2
workers = int(os.environ.get("GUNICORN_WORKERS", (2 * _cores) + 1))

# Kill and restart a worker that takes longer than this (seconds) on a request.
timeout = 60

# Disable gunicorn's control socket. It defaults to writing a unix socket under
# $HOME/.gunicorn, which fails on the read-only root filesystem the container
# runs with (see docker-compose.selfhost.yml). We manage the process via Docker, not
# the `gunicorn ctl` CLI, so the control interface is unused — disabling it drops
# a write path and an attack surface rather than mounting a tmpfs to support it.
control_socket_disable = True

# No access log: kamal-proxy sits in front of every request and already logs
# it (status, path, duration, method) as structured JSON with a real level --
# gunicorn's own combined-log-format access line would just duplicate that,
# minus the level. Error log still goes to stdout so Docker captures it.
accesslog = None
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Gunicorn boots this file directly, well before Django's own settings module
# (and its structlog.configure() call) is ever imported, so this can't share
# that config -- it rebuilds the same JSON-in-prod/console-in-dev formatter
# gunicorn-side instead, keeping this the one log source left that wasn't
# structured. `config.update(logconfig_dict)` on gunicorn's side is a shallow
# top-level dict update, not a deep merge, so "handlers" here must redefine
# both of gunicorn's default handlers (not just error_console) or "console"
# -- still referenced by its own "root"/"gunicorn.access" logger entries --
# would vanish and dictConfig would fail at boot.
_gunicorn_log_is_debug = (
    os.environ.get("DJANGO_SETTINGS_MODULE", "root.settings") != "root.settings_prod"
)
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structlog": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processors": [
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer()
                if _gunicorn_log_is_debug
                else structlog.processors.JSONRenderer(),
            ],
            "foreign_pre_chain": [
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
            ],
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structlog",
            "stream": "ext://sys.stdout",
        },
        "error_console": {
            "class": "logging.StreamHandler",
            "formatter": "structlog",
            "stream": "ext://sys.stderr",
        },
    },
}
