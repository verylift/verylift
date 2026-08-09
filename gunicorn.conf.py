"""Gunicorn configuration for very lift production deployment."""

import multiprocessing
import os

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

# Access and error logs to stdout/stderr so Docker captures them.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
