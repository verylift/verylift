"""URL configuration for the accounts app."""

from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("accounts/login/", views.LocalLoginView.as_view(), name="login"),
    path("accounts/register/", views.register_view, name="register"),
    path(
        "accounts/password-reset/",
        views.password_reset_view,
        name="password-reset",
    ),
    path(
        "accounts/password-reset/done/",
        views.password_reset_done_view,
        name="password-reset-done",
    ),
    # Deliberately short (not under password-reset/) because it goes in an email
    # body, where a long URL gets line-wrapped and the link breaks.
    path(
        "accounts/reset/<uidb64>/<token>/",
        views.password_reset_confirm_view,
        name="password-reset-confirm",
    ),
    path("settings/", views.settings_view, name="settings"),
    path("settings/sync-now/", views.sync_now_view, name="sync_now"),
    path(
        "settings/validate-liftosaur-key/",
        views.validate_liftosaur_key_view,
        name="validate_liftosaur_key",
    ),
    path("tz/detect/", views.timezone_detect_view, name="timezone-detect"),
]
