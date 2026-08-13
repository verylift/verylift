"""URL configuration for the policies app."""

from django.urls import path

from policies import views

app_name = "policies"

urlpatterns = [
    path("", views.policy_list_view, name="list"),
    path("consent/", views.consent_view, name="consent"),
    path("<slug:slug>/", views.policy_detail_view, name="detail"),
]
