"""Shared read helpers over the per-tracker sync logs.

Each tracker integration keeps its own sync-log model -- LiftosaurSyncLog,
WgerSyncLog, HevySyncLog -- with the same (user, started_at, success,
error_detail) shape. The question "did this user's last sync actually work"
has one answer for all of them, so it lives here once rather than being
re-derived per app. Takes the model as an argument so core stays free of
imports from the integration apps.

A fuller per-connector contract is TASK-333's concern; this is deliberately
just the one read every tracker's settings UI needs.
"""

import logging

logger = logging.getLogger(__name__)


def latest_sync_failure(log_model, user):
    """Return the user's most recent sync log, if that attempt failed.

    ``None`` when the most recent attempt succeeded, is still in progress
    (``success=None``), or none has ever run. This is the "did the last sync
    actually work" signal -- distinct from a ``last_synced_at`` stamp, which
    only tracks successes and so cannot tell a run that failed outright from
    one that had nothing to pull: every tracker's sync primitive swallows
    API/network/DB-contention failures internally and returns 0 for both, so
    callers that want to report the difference must read the log rather than
    the return value.

    Most-recent-log-wins is the point: filtering on ``success=False`` alone
    would nag a recovered user forever.
    """
    log = log_model.objects.filter(user=user).order_by("-started_at").first()
    if log is not None and log.success is False:
        return log
    return None
