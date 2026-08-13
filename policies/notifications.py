import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

from policies.models import PolicyConsent, PolicyNotification

logger = logging.getLogger(__name__)


def notify_users_for_version(version, request, dry_run=False):
    """Email every active user who hasn't consented to or been notified of `version`.

    `request` is required to build an absolute consent URL -- this is only ever
    triggered from the admin action, which always has one.

    Returns the count of notifications sent (or that would be sent, under
    dry_run).
    """
    User = get_user_model()
    consented_ids = PolicyConsent.objects.filter(policy_version=version).values_list(
        "user_id", flat=True
    )
    notified_ids = PolicyNotification.objects.filter(
        policy_version=version
    ).values_list("user_id", flat=True)

    users = (
        User.objects.filter(is_active=True)
        .exclude(id__in=consented_ids)
        .exclude(id__in=notified_ids)
        .exclude(email="")
    )

    consent_url = request.build_absolute_uri(reverse("policies:consent"))
    context = {"version": version, "consent_url": consent_url}
    subject = render_to_string(
        "policies/email/policy_notification_subject.txt", context
    ).strip()
    body = render_to_string("policies/email/policy_notification.txt", context)

    count = 0
    for user in users:
        if not dry_run:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email])
            PolicyNotification.objects.create(
                user=user,
                policy_version=version,
                method=PolicyNotification.Method.EMAIL,
            )
        count += 1

    logger.info(
        "%s %d policy notification(s) for %s",
        "Would send" if dry_run else "Sent",
        count,
        version,
    )
    return count
