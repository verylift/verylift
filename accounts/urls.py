"""URL configuration for the accounts app."""

from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("accounts/login/", views.LocalLoginView.as_view(), name="login"),
    path("accounts/register/", views.register_view, name="register"),
    path(
        "accounts/onboarding/tracking-method/",
        views.onboarding_tracking_method_view,
        name="onboarding-tracking-method",
    ),
    path(
        "accounts/onboarding/connect/<str:app>/",
        views.onboarding_connect_tracker_view,
        name="onboarding-connect-tracker",
    ),
    path(
        "accounts/onboarding/other-tracker/",
        views.onboarding_other_tracker_view,
        name="onboarding-other-tracker",
    ),
    path(
        "accounts/onboarding/no-tracker/",
        views.onboarding_no_tracker_view,
        name="onboarding-no-tracker",
    ),
    path(
        "accounts/onboarding/units/",
        views.onboarding_units_view,
        name="onboarding-units",
    ),
    path(
        "accounts/onboarding/bodyweight/",
        views.onboarding_bodyweight_view,
        name="onboarding-bodyweight",
    ),
    path(
        "accounts/onboarding/very-open/",
        views.onboarding_very_open_view,
        name="onboarding-very-open",
    ),
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
    path(
        "settings/delete-account/",
        views.delete_account_view,
        name="delete-account",
    ),
    path("settings/sync-now/", views.sync_now_view, name="sync_now"),
    path(
        "settings/validate-liftosaur-key/",
        views.validate_liftosaur_key_view,
        name="validate_liftosaur_key",
    ),
    path("settings/wger-sync-now/", views.wger_sync_now_view, name="wger_sync_now"),
    path(
        "settings/validate-wger-credentials/",
        views.validate_wger_credentials_view,
        name="validate_wger_credentials",
    ),
    path("settings/hevy-sync-now/", views.hevy_sync_now_view, name="hevy_sync_now"),
    path(
        "settings/validate-hevy-key/",
        views.validate_hevy_key_view,
        name="validate_hevy_key",
    ),
    path("tz/detect/", views.timezone_detect_view, name="timezone-detect"),
]
