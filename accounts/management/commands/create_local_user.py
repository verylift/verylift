"""Management command to create a local (non-SSO) user for development."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Create or update a local user with a usable password (dev only)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--email", default="")
        parser.add_argument("--display-name", default="")

    def handle(self, *args, **options):
        username = options["username"]
        password = options["password"]
        email = options["email"]
        display_name = options["display_name"] or username

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "email": email,
                "display_name": display_name,
                "acquisition_source": User.AcquisitionSource.ADMIN,
            },
        )
        user.set_password(password)
        if not created:
            if email:
                user.email = email
            if options["display_name"]:
                user.display_name = options["display_name"]
        user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} user '{username}'."))
