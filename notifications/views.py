"""Views for the notifications app."""

import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from challenges.models import ChallengeParticipant
from core.http import is_htmx
from notifications.models import Notification

logger = logging.getLogger(__name__)

SECTION_LIMIT = 30


def build_display_text(notification):
    """Return the human-readable text for a notification row."""
    challenge_name = (
        notification.challenge.name
        if notification.challenge is not None
        else gettext("a challenge")
    )
    metadata = notification.metadata or {}
    event_type = notification.event_type

    if event_type == Notification.EventType.INVITE_RECEIVED:
        return gettext("You have been invited to %(challenge)s") % {
            "challenge": challenge_name
        }
    if event_type == Notification.EventType.OWNERSHIP_TRANSFERRED:
        return gettext("You are now the owner of %(challenge)s") % {
            "challenge": challenge_name
        }
    if event_type == Notification.EventType.USER_JOINED:
        joined_name = metadata.get("joined_user_name") or gettext("Someone")
        return gettext("%(name)s joined %(challenge)s") % {
            "name": joined_name,
            "challenge": challenge_name,
        }
    if event_type == Notification.EventType.OVERTAKEN:
        overtaken_by = metadata.get("overtaken_by_name") or gettext("Someone")
        return gettext("%(name)s passed you in %(challenge)s") % {
            "name": overtaken_by,
            "challenge": challenge_name,
        }
    if event_type == Notification.EventType.CHALLENGE_CLOSED:
        return gettext("%(challenge)s has ended — view your final placing") % {
            "challenge": challenge_name
        }
    if event_type == Notification.EventType.REMOVED_FROM_CHALLENGE:
        return gettext("You have been removed from %(challenge)s") % {
            "challenge": challenge_name
        }
    return ""


def _target_url(notification):
    """Return the deep-link URL a notification should navigate to once read.

    An invite_received deep-link only ever comes from a legacy row now (TASK-272
    removed the user-search invite lifecycle, so nothing creates these any
    more). It routes to the challenge detail page when the user is an accepted
    member and to the dashboard otherwise — detail would raise PermissionDenied
    (403) for anyone else, and there is no invite card left to anchor to.

    A removed-from-challenge notification routes to the dashboard for the same
    reason: a removed user is no longer an accepted participant.
    """
    if notification.challenge is None:
        return reverse("challenges:dashboard")
    if notification.event_type == Notification.EventType.REMOVED_FROM_CHALLENGE:
        return reverse("challenges:dashboard")
    if notification.event_type == Notification.EventType.INVITE_RECEIVED:
        is_accepted = ChallengeParticipant.objects.filter(
            challenge=notification.challenge,
            user=notification.user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        ).exists()
        if is_accepted:
            return reverse("challenges:detail", args=[notification.challenge.pk])
        return reverse("challenges:dashboard")
    return reverse("challenges:detail", args=[notification.challenge.pk])


def _notification_row(notification):
    """Row-shape dict for one notification: the object plus its display text."""
    return {"notification": notification, "text": build_display_text(notification)}


def dashboard_section_context(user, show_read=False):
    """Context for the dashboard's notifications section.

    Newest first, capped at SECTION_LIMIT rows — the section is a scrollable
    box, not a paginated page. Backs the dashboard view, the section-toggle
    partial, and the mark-all-read htmx partial, which all render identical
    rows/unread-count context.

    Defaults to unread-only (``show_read=False``); read notifications used to
    accumulate and crowd out unread ones in the scrollable box. ``show_read``
    reveals them. ``unread_notification_count`` and ``has_notifications`` are
    always computed against the full set, independent of ``show_read``, since
    they drive the "Mark all as read" and "Show/Hide read" controls, which
    must stay visible/accurate regardless of which rows are currently shown.
    """
    base_queryset = Notification.objects.filter(user=user).select_related("challenge")
    has_notifications = base_queryset.exists()
    queryset = base_queryset if show_read else base_queryset.filter(is_read=False)
    notifications = queryset[:SECTION_LIMIT]
    return {
        "rows": [_notification_row(notification) for notification in notifications],
        "unread_notification_count": Notification.objects.filter(
            user=user, is_read=False
        ).count(),
        "show_read": show_read,
        "has_notifications": has_notifications,
    }


@login_required
def notification_section_view(request):
    """Re-render the dashboard's notifications section in a different
    read-visibility state (``?show_read=1`` or omitted/``0``).

    This is a partial-fetch toggle, not a standalone notifications page —
    TASK-246 deliberately removed that page, and this endpoint's only job is
    to swap the section in place, matching the settings page's edit-toggle
    GET pattern (e.g. ``rename_challenge_view``). HTMX requests get the
    section partial; a plain request (no JS) redirects to the dashboard
    carrying the same state via the same ``?show_read=`` query param
    ``dashboard_view`` honours on full load — the bookmarkable-query-param
    fallback ``find_challenges_view`` also uses for ``hide_completed``.
    """
    show_read = request.GET.get("show_read") == "1"
    if is_htmx(request):
        context = dashboard_section_context(request.user, show_read=show_read)
        return render(request, "notifications/_notification_list.html", context)
    url = reverse("challenges:dashboard")
    if show_read:
        url += "?show_read=1"
    return redirect(url)


@login_required
@require_POST
def mark_read_view(request, pk):
    """Mark a single notification read, then navigate to its deep-link target.

    Every click on a notification is an HTMX request (the row is rendered as a
    button with hx-post), so both HTMX and plain requests navigate to the
    notification's deep-link target: HTMX gets a 204 with an HX-Redirect header
    for a client-side navigation, plain requests get a 302.
    """
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=["is_read"])
        logger.info(
            "User %s marked notification %s read", request.user.id, notification.pk
        )

    url = _target_url(notification)
    if is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


@login_required
@require_POST
def mark_all_read_view(request):
    """Mark all of the requesting user's unread notifications as read.

    HTMX requests get the notifications section partial (with out-of-band
    messages) at HTTP 200 so the dashboard section updates in place; plain
    requests redirect to the dashboard, where the section lives. Preserves
    the current ``?show_read=`` state from the request so marking all as read
    from the "show read" view doesn't unexpectedly hide everything the user
    was just looking at — from the default unread-only view it correctly
    empties the section down to the "You're all caught up" state.
    """
    show_read = request.GET.get("show_read") == "1"
    updated = Notification.objects.filter(user=request.user, is_read=False).update(
        is_read=True
    )
    logger.info(
        "User %s marked %s notification(s) read via mark-all", request.user.id, updated
    )

    if is_htmx(request):
        # Rebuild the section to show the updated state
        context = dashboard_section_context(request.user, show_read=show_read)
        context["oob_messages"] = True
        return render(request, "notifications/_notification_list.html", context)

    url = reverse("challenges:dashboard")
    if show_read:
        url += "?show_read=1"
    return redirect(url)
