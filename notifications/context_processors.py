"""Template context processors for the notifications app."""

from notifications.models import Notification


def unread_notification_count(request):
    """Expose the requesting user's unread notification count to every template."""
    if not request.user.is_authenticated:
        return {"unread_notification_count": 0}
    count = Notification.objects.filter(user=request.user, is_read=False).count()
    return {"unread_notification_count": count}
