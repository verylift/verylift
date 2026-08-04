"""URL configuration for the notifications app."""

from django.urls import path

from notifications import views

app_name = "notifications"

urlpatterns = [
    path(
        "notifications/section/",
        views.notification_section_view,
        name="section",
    ),
    path(
        "notifications/<uuid:pk>/read/",
        views.mark_read_view,
        name="read",
    ),
    path(
        "notifications/read-all/",
        views.mark_all_read_view,
        name="read-all",
    ),
]
