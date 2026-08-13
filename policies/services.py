import logging

from policies.models import PolicyConsent, PolicyVersion

logger = logging.getLogger(__name__)


def pending_versions_for(user):
    """Active, gated policy versions `user` has not yet consented to."""
    required_pks = list(
        PolicyVersion.objects.active_gated().values_list("pk", flat=True)
    )
    if not required_pks:
        return PolicyVersion.objects.none()
    consented_pks = set(
        user.policy_consents.filter(policy_version_id__in=required_pks).values_list(
            "policy_version_id", flat=True
        )
    )
    pending_pks = set(required_pks) - consented_pks
    return PolicyVersion.objects.filter(pk__in=pending_pks).select_related("policy")


def record_consent(user, request, versions, method):
    """Record `user`'s consent to each of `versions`, idempotently."""
    ip_address = request.META.get("REMOTE_ADDR")
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    for version in versions:
        PolicyConsent.objects.get_or_create(
            user=user,
            policy_version=version,
            defaults={
                "ip_address": ip_address,
                "user_agent": user_agent,
                "method": method,
            },
        )
    logger.info(
        "Recorded %s consent for user %s (%d version(s))",
        method,
        user.id,
        len(versions) if hasattr(versions, "__len__") else versions.count(),
    )
