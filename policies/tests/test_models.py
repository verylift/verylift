import pytest
from django.db import IntegrityError

from policies.models import PolicyConsent, PolicyVersion
from policies.tests.factories import (
    PolicyConsentFactory,
    PolicyFactory,
    PolicyVersionFactory,
)

pytestmark = pytest.mark.django_db


class TestPolicyVersionActivation:
    def test_activating_a_version_deactivates_the_prior_active_version(self):
        policy = PolicyFactory()
        old = PolicyVersionFactory(policy=policy, version="1.0", is_active=True)
        new = PolicyVersionFactory(policy=policy, version="2.0", is_active=True)

        old.refresh_from_db()
        assert old.is_active is False
        assert new.is_active is True

    def test_activating_a_version_does_not_affect_other_policies(self):
        other_policy_version = PolicyVersionFactory(is_active=True)
        policy = PolicyFactory()
        PolicyVersionFactory(policy=policy, is_active=True)

        other_policy_version.refresh_from_db()
        assert other_policy_version.is_active is True


class TestPolicyVersionQuerySet:
    def test_active_gated_excludes_versions_whose_policy_does_not_gate_access(self):
        policy = PolicyFactory(gates_access=False)
        version = PolicyVersionFactory(policy=policy, is_active=True)

        assert version not in PolicyVersion.objects.active_gated()
        assert version in PolicyVersion.objects.active_requiring_consent()

    def test_active_gated_excludes_versions_that_do_not_require_consent(self):
        policy = PolicyFactory(requires_consent=False, gates_access=True)
        version = PolicyVersionFactory(policy=policy, is_active=True)

        assert version not in PolicyVersion.objects.active_gated()

    def test_active_gated_excludes_inactive_versions(self):
        policy = PolicyFactory()
        version = PolicyVersionFactory(policy=policy, is_active=False)

        assert version not in PolicyVersion.objects.active_gated()

    def test_active_gated_includes_a_gated_active_version(self):
        policy = PolicyFactory(requires_consent=True, gates_access=True)
        version = PolicyVersionFactory(policy=policy, is_active=True)

        assert version in PolicyVersion.objects.active_gated()


class TestPolicyConsentUniqueness:
    def test_a_user_cannot_consent_to_the_same_version_twice(self):
        consent = PolicyConsentFactory()

        with pytest.raises(IntegrityError):
            PolicyConsent.objects.create(
                user=consent.user,
                policy_version=consent.policy_version,
                method=PolicyConsent.Method.RE_CONSENT,
            )
