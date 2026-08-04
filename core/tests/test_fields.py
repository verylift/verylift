"""Tests for core.fields.EncryptedCharField (TASK-285).

Uses accounts.User.liftosaur_api_key as the field's only current consumer, so
these are also the acceptance tests for "the key is encrypted at rest".
"""

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.db import connection

from accounts.tests.factories import UserFactory
from core.encryption import decrypt

User = get_user_model()

PLAINTEXT_KEY = "liftosaur-plaintext-key-abc123"


@pytest.fixture
def pinned_key(settings):
    settings.FIELD_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    return settings


def _raw_column_value(user):
    """The column as Postgres holds it, bypassing from_db_value entirely.

    Reading through the ORM would prove nothing about what is stored: the field
    decrypts on the way out, so an unencrypted column would look identical.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT liftosaur_api_key FROM accounts_user WHERE id = %s", [user.id]
        )
        return cursor.fetchone()[0]


@pytest.mark.django_db
class TestStoredValueIsEncrypted:
    def test_column_holds_ciphertext_not_plaintext(self, pinned_key):
        user = UserFactory(liftosaur_api_key=PLAINTEXT_KEY)

        stored = _raw_column_value(user)

        assert stored != PLAINTEXT_KEY
        assert PLAINTEXT_KEY not in stored
        assert decrypt(stored) == PLAINTEXT_KEY

    def test_orm_read_returns_plaintext(self, pinned_key):
        user = UserFactory(liftosaur_api_key=PLAINTEXT_KEY)

        assert User.objects.get(pk=user.pk).liftosaur_api_key == PLAINTEXT_KEY

    def test_update_reencrypts(self, pinned_key):
        user = UserFactory(liftosaur_api_key=PLAINTEXT_KEY)

        user.liftosaur_api_key = "second-key"
        user.save(update_fields=["liftosaur_api_key"])

        assert decrypt(_raw_column_value(user)) == "second-key"
        user.refresh_from_db()
        assert user.liftosaur_api_key == "second-key"

    def test_queryset_update_encrypts(self, pinned_key):
        user = UserFactory(liftosaur_api_key=None)

        User.objects.filter(pk=user.pk).update(liftosaur_api_key="via-update")

        assert decrypt(_raw_column_value(user)) == "via-update"

    def test_ciphertext_of_a_max_length_plaintext_fits_the_column(self, pinned_key):
        # The column bounds the ciphertext, and 255 chars was the old plaintext
        # bound -- a too-narrow column would raise a DataError here.
        long_key = "k" * 255
        user = UserFactory(liftosaur_api_key=long_key)

        assert len(_raw_column_value(user)) <= 600
        user.refresh_from_db()
        assert user.liftosaur_api_key == long_key


@pytest.mark.django_db
class TestFalsyValuesPassThrough:
    def test_none_round_trips(self, pinned_key):
        user = UserFactory(liftosaur_api_key=None)

        assert _raw_column_value(user) is None
        user.refresh_from_db()
        assert user.liftosaur_api_key is None
        assert bool(user.liftosaur_api_key) is False

    def test_empty_string_round_trips(self, pinned_key):
        user = UserFactory(liftosaur_api_key="")

        assert _raw_column_value(user) == ""
        user.refresh_from_db()
        assert user.liftosaur_api_key == ""


@pytest.mark.django_db
class TestLookupsAreRefused:
    def test_exact_lookup_raises(self, pinned_key):
        UserFactory(liftosaur_api_key=PLAINTEXT_KEY)

        # Silently matching zero rows is the failure mode being prevented.
        with pytest.raises(NotImplementedError, match="non-deterministic"):
            list(User.objects.filter(liftosaur_api_key=PLAINTEXT_KEY))

    def test_contains_lookup_raises(self, pinned_key):
        with pytest.raises(NotImplementedError):
            list(User.objects.filter(liftosaur_api_key__contains="lift"))

    def test_isnull_lookup_works(self, pinned_key):
        with_key = UserFactory(liftosaur_api_key=PLAINTEXT_KEY)
        without_key = UserFactory(liftosaur_api_key=None)

        assert list(
            User.objects.filter(liftosaur_api_key__isnull=True).values_list(
                "pk", flat=True
            )
        ) == [without_key.pk]
        assert with_key.pk not in User.objects.filter(
            liftosaur_api_key__isnull=True
        ).values_list("pk", flat=True)
