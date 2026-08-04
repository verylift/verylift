"""Model fields backed by core.encryption (TASK-285)."""

from django.db import models

from core.encryption import decrypt, encrypt


class EncryptedCharField(models.CharField):
    """CharField whose value is Fernet-encrypted at rest.

    Reads and writes are transparent: the Python attribute is always plaintext,
    the column always ciphertext. Two consequences are not obvious:

    * ``max_length`` bounds the *ciphertext* in the column, while the inherited
      ``MaxLengthValidator`` applies to the *plaintext*. Fernet expands input by
      roughly 4/3 plus ~100 bytes of header/HMAC, so ``max_length`` here is a
      deliberately loose upper bound, not a plaintext cap. Cap plaintext length
      in the form if a cap is wanted.
    * ``dumpdata`` emits *decrypted* plaintext, because the inherited
      ``value_to_string`` reads the model attribute. That is deliberate so
      ``loaddata`` round-trips (it goes through ``to_python`` and then
      ``get_prep_value``, which re-encrypts); emitting ciphertext instead would
      double-encrypt on load. Consequence: any fixture dumped from a model with
      this field contains secrets in the clear.

    Falsy values (``None``, ``""``) pass through unencrypted so truthiness tests
    on the attribute keep their meaning.
    """

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        return decrypt(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return encrypt(value)

    def get_lookup(self, lookup_name):
        # Fernet ciphertext is non-deterministic (random IV plus a timestamp),
        # so encrypting the right-hand side of a lookup can never match the
        # stored bytes: a value lookup would silently return zero rows. Raise
        # instead -- a silently-empty queryset on a credential column is exactly
        # the bug this is guarding against. `isnull` is safe: it never touches
        # the value.
        if lookup_name != "isnull":
            raise NotImplementedError(
                f"{type(self).__name__} cannot be used in a "
                f"'{lookup_name}' lookup: the stored ciphertext is "
                "non-deterministic, so any value lookup would match nothing. "
                "Filter on a separate plaintext column, or load the rows and "
                "compare in Python. Only 'isnull' is supported."
            )
        return super().get_lookup(lookup_name)
