import pytest
from django.db import IntegrityError

from core.models import NewsletterSubscriber


@pytest.mark.django_db
def test_email_uniqueness_is_enforced_at_the_db_level():
    NewsletterSubscriber.objects.create(email="dup@example.com")
    with pytest.raises(IntegrityError):
        NewsletterSubscriber.objects.create(email="dup@example.com")
