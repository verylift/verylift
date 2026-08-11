from unittest.mock import patch

import pytest
from django.test import Client


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
def test_healthz_returns_200_when_db_reachable(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"


@pytest.mark.django_db
def test_healthz_includes_app_version(client, settings):
    settings.APP_VERSION = "2026.8.4"
    response = client.get("/healthz")
    assert response.json()["version"] == "2026.8.4"


@pytest.mark.django_db
def test_healthz_requires_no_auth(client):
    # No login performed; the endpoint must still answer anonymously.
    response = client.get("/healthz")
    assert response.status_code == 200


def test_healthz_returns_503_when_db_unreachable(client):
    with patch("core.middleware.connection.cursor", side_effect=OSError("boom")):
        response = client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["database"] == "down"
    assert "version" in body


@pytest.mark.django_db
def test_healthz_reachable_from_non_loopback(client):
    # Under Kamal, kamal-proxy reaches this endpoint from its own container IP,
    # never loopback — the check must still succeed.
    response = client.get("/healthz", REMOTE_ADDR="203.0.113.7")
    assert response.status_code == 200


@pytest.mark.django_db
def test_healthz_allows_ipv6_loopback(client):
    response = client.get("/healthz", REMOTE_ADDR="::1")
    assert response.status_code == 200
