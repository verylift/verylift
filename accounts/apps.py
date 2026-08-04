from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        import accounts.checks  # noqa: F401  -- registers system checks
        import accounts.signals  # noqa: F401
