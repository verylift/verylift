"""URL configuration for the guide app."""

from django.urls import path

from guide import views

app_name = "guide"

urlpatterns = [
    path("docs/", views.index_view, name="index"),
    path("docs/<slug:slug>/", views.page_view, name="page"),
]
