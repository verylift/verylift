import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from accounts.models import User

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=User)
def delete_avatar_file(sender, instance, **kwargs):
    """Remove the avatar file from storage when its owning User row is gone.

    post_delete (not pre_delete) so the file is only removed once the row
    deletion has actually happened; save=False since instance no longer has
    a row to write back to.
    """
    if instance.avatar:
        instance.avatar.delete(save=False)
