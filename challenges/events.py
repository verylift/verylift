"""Challenge activity log: recording events, and reading them back.

A separate module from ``challenges.services`` so the goal-saving modules
(``challenges.custom_goals``, ``challenges.rep_target_goals``) can record an
event without importing ``services``, which imports them -- the cycle that
would otherwise force the goal-locked entry to be emitted from the view
instead of from the function that actually locks the goal.
"""

import logging
from datetime import UTC, datetime, time

from django.db import transaction
from django.utils.translation import gettext

from challenges.models import ChallengeEvent

logger = logging.getLogger(__name__)

# How many merged entries the Settings page's log renders. No pagination in
# V1: this is a "what happened here lately" panel, not an audit export.
EVENT_LOG_LIMIT = 50


def record_challenge_event(challenge, event_type, *, actor=None, **metadata):
    """Append one :class:`~challenges.models.ChallengeEvent` to a challenge's log.

    ``actor`` is the person the event is about -- the joiner, the leaver, the
    person removed, the new owner -- not necessarily whoever triggered it.
    Pass no name in ``metadata``: see the model docstring for why a name
    snapshot here would defeat account deletion.

    Never raises. A log entry is a side effect of an action that has already
    happened (and, at every call site, has committed or is committing in the
    same transaction); failing to record it must not fail the join, the bail,
    or the close it describes. Failures are logged with the event attached so
    a gap in the log is traceable.

    The inner ``atomic()`` is what makes "never raises" actually true at the
    call sites that run inside a transaction (save_custom_goal,
    remove_participant, transfer_ownership). Catching a database error without
    it would leave the enclosing transaction broken, and the next query -- or
    the commit itself -- would raise TransactionManagementError, turning a
    missing log line into the failed action this promises not to cause. The
    savepoint rolls back just this insert and leaves the outer transaction
    usable.
    """
    try:
        with transaction.atomic():
            return ChallengeEvent.objects.create(
                challenge=challenge,
                event_type=event_type,
                actor=actor,
                metadata=metadata,
            )
    except Exception:
        logger.exception(
            "Failed to record %s event on challenge %s (actor %s)",
            event_type,
            challenge.pk,
            getattr(actor, "pk", None),
        )
        return None


def actor_label(user) -> str:
    """The name to show for an event's actor.

    A deactivated (self-serve-deleted) account renders as a neutral
    placeholder, never as the pseudonym ``anonymize_account`` left behind --
    the log exists to say *what happened*, and "a deleted account left" says
    it without reintroducing the fake-looking human name this branch removed
    from every other surface. A null actor (a user row hard-deleted out from
    under a SET_NULL FK) renders the same way; there is nothing left to name.
    """
    if user is None or not user.is_active:
        return gettext("a deleted account")
    return user.effective_display_name


def build_challenge_event_log(challenge, *, limit=EVENT_LOG_LIMIT):
    """Merged, newest-first activity log for a challenge's Settings page.

    Two sources, one timeline:

    - stored ``ChallengeEvent`` rows (membership and lifecycle), which exist
      only from when an action actually happened; and
    - scoring entries derived on the fly from ``PointEarnEvent`` via
      ``scoring.services.iter_scoring_sessions``, so the log carries a
      challenge's whole scoring history rather than only what postdates this
      feature, and so there is one scoring truth rather than two.

    Scoring entries include departed lifters (bailed, removed, deleted):
    unlike the leaderboard, this is a history of what happened, and "a deleted
    account scored 6 on Squat" followed by "a deleted account left" is the
    sequence the owner is here to read.

    A ``PointEarnEvent`` carries only a ``performed_at`` date, so it sorts into
    the timeline at the start of that day -- a set lands before the same day's
    membership changes. That is a deliberate approximation: the alternative,
    ``synced_at``, is when a tracker happened to be polled, which would order
    the log by an artifact of syncing rather than by when anyone lifted.

    Takes no viewing user: unlike Recent Activity it prints no weights, so
    there is no unit preference to honour and the log is identical for every
    viewer of the Settings page.

    Each entry: ``{"kind": str, "actor": str, "at": datetime, "detail": dict}``.
    """
    from scoring.services import iter_scoring_sessions

    entries = [
        {
            "kind": event.event_type,
            "actor": actor_label(event.actor),
            "at": event.created_at,
            "detail": event.metadata,
        }
        for event in ChallengeEvent.objects.filter(challenge=challenge).select_related(
            "actor"
        )[:limit]
    ]

    # Both sources are already newest-first, so only the newest `limit` of each
    # can possibly survive into the newest `limit` of the merge -- slicing here
    # bounds the work rather than sorting a whole challenge's history to throw
    # nearly all of it away.
    for event, points_delta in iter_scoring_sessions(challenge, include_departed=True)[
        :limit
    ]:
        entries.append(
            {
                "kind": "scored",
                "actor": actor_label(event.user),
                "at": datetime.combine(event.performed_at, time.min, tzinfo=UTC),
                "detail": {"lift": event.lift, "points": points_delta},
            }
        )

    entries.sort(key=lambda entry: entry["at"], reverse=True)
    return entries[:limit]
