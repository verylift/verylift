import factory

from policies.models import Policy, PolicyConsent, PolicyNotification, PolicyVersion


class PolicyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Policy

    name = factory.Sequence(lambda n: f"Test Policy {n}")
    slug = factory.Sequence(lambda n: f"test-policy-{n}")
    policy_type = Policy.PolicyType.OTHER
    requires_consent = True
    gates_access = True


class PolicyVersionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PolicyVersion

    policy = factory.SubFactory(PolicyFactory)
    version = "1.0"
    url = "https://example.com/policy"
    effective_date = "2026-01-01"
    is_active = True


class PolicyConsentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PolicyConsent

    user = factory.SubFactory("accounts.tests.factories.UserFactory")
    policy_version = factory.SubFactory(PolicyVersionFactory)
    method = PolicyConsent.Method.SIGNUP


class PolicyNotificationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PolicyNotification

    user = factory.SubFactory("accounts.tests.factories.UserFactory")
    policy_version = factory.SubFactory(PolicyVersionFactory)
    method = PolicyNotification.Method.EMAIL
