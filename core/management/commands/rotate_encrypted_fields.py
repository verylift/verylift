"""Re-encrypt EncryptedCharField rows under the current key (TASK-289).

TASK-285's MultiFernet setup lets retired keys keep decrypting old rows, but
nothing re-encrypts those rows under the new current key -- a retired key must
stay in FIELD_ENCRYPTION_KEYS forever unless something rewrites every row. This
command is that something.

Two non-obvious choices, mirroring
accounts/migrations/0014_encrypt_existing_liftosaur_keys.py:

* A raw cursor, not the ORM. Every ORM read runs ``from_db_value`` (decrypts)
  and every ORM write runs ``get_prep_value`` (encrypts); going through the
  model would decrypt-then-encrypt a value this command has already rotated
  via ``core.encryption.rotate``, encrypting it twice. The cursor sees the
  column as it really is.
* Every non-empty row is rotated unconditionally, with no check for "already
  on the current key". ``rotate()`` succeeds regardless of which configured
  key a row is currently under, so re-running is inherently safe: a row
  already on the current key is simply rewritten to a new, equally valid
  ciphertext rather than skipped. There is no unsafe state to detect.

Generic over models: it discovers every ``core.fields.EncryptedCharField`` on
every installed model, so adding a second encrypted field or model needs no
change here.
"""

import logging

from cryptography.fernet import InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection

from core.encryption import rotate
from core.fields import EncryptedCharField

logger = logging.getLogger(__name__)


def _encrypted_fields():
    for model in apps.get_models():
        for field in model._meta.local_fields:
            if isinstance(field, EncryptedCharField):
                yield model, field


class Command(BaseCommand):
    help = (
        "Re-encrypt every EncryptedCharField row under the current (first) "
        "FIELD_ENCRYPTION_KEYS entry. Run once after prepending a new key and "
        "before dropping a retired one."
    )

    def handle(self, *args, **options):
        total = 0
        for model, field in _encrypted_fields():
            count = self._rotate_field(model, field)
            total += count
            self.stdout.write(
                f"{model._meta.label}.{field.name}: rotated {count} row(s)."
            )

        logger.info("rotate_encrypted_fields rotated %d row(s) total", total)
        self.stdout.write(self.style.SUCCESS(f"Done. Rotated {total} row(s) total."))

    def _rotate_field(self, model, field):
        table = model._meta.db_table
        column = field.column
        pk_column = model._meta.pk.column
        select = (
            f"SELECT {pk_column}, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} <> ''"
        )
        update = f"UPDATE {table} SET {column} = %s WHERE {pk_column} = %s"

        count = 0
        with connection.cursor() as cursor:
            cursor.execute(select)
            rows = cursor.fetchall()

            for pk, value in rows:
                try:
                    new_value = rotate(value)
                except InvalidToken:
                    logger.exception(
                        "Failed to rotate %s.%s for pk=%s", table, column, pk
                    )
                    raise
                cursor.execute(update, [new_value, pk])
                count += 1
        return count
