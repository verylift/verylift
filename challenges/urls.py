"""URL configuration for the challenges app."""

from django.urls import path

from challenges import views

app_name = "challenges"


urlpatterns = [
    path("dashboard/", views.dashboard_view, name="dashboard"),
    # Top-level and pk-free (not nested under challenges/<uuid:pk>/): short
    # enough to paste into a group chat, and leaks no challenge identifier.
    path("join/<str:token>/", views.invite_link_view, name="invite-link"),
    path("challenges/create/", views.create_challenge_view, name="create"),
    path("challenges/", views.find_challenges_view, name="find"),
    path(
        "challenges/<uuid:pk>/",
        views.challenge_detail_view,
        name="detail",
    ),
    path(
        "challenges/<uuid:pk>/settings/",
        views.challenge_settings_view,
        name="settings",
    ),
    path(
        "challenges/<uuid:pk>/goal-setup/",
        views.goal_setup_view,
        name="goal-setup",
    ),
    path(
        "challenges/<uuid:pk>/bail/",
        views.bail_view,
        name="bail",
    ),
    path(
        "challenges/<uuid:pk>/invite-link/",
        views.regenerate_invite_link_view,
        name="regenerate-invite-link",
    ),
    path(
        "challenges/<uuid:pk>/invite-link/update/",
        views.update_invite_link_view,
        name="update-invite-link",
    ),
    path(
        "challenges/<uuid:pk>/share/",
        views.share_challenge_view,
        name="share",
    ),
    path(
        "challenges/<uuid:pk>/remove/<uuid:participant_pk>/",
        views.remove_participant_view,
        name="remove",
    ),
    path(
        "challenges/<uuid:pk>/participant/<uuid:participant_pk>/chart/",
        views.participant_chart_view,
        name="participant-chart",
    ),
    path(
        "challenges/<uuid:pk>/manual-lift/",
        views.manual_lift_view,
        name="manual-lift",
    ),
    path(
        "challenges/<uuid:pk>/transfer/<uuid:user_id>/",
        views.transfer_ownership_view,
        name="transfer",
    ),
    path(
        "challenges/<uuid:pk>/close/",
        views.close_challenge_view,
        name="close",
    ),
    path(
        "challenges/<uuid:pk>/rename/",
        views.rename_challenge_view,
        name="rename",
    ),
    path(
        "challenges/<uuid:pk>/history-window/",
        views.history_window_view,
        name="history-window",
    ),
    path(
        "challenges/<uuid:pk>/cancel/",
        views.cancel_challenge_view,
        name="cancel",
    ),
    path(
        "challenges/<uuid:pk>/delete-draft/",
        views.delete_draft_challenge_view,
        name="delete-draft",
    ),
]
