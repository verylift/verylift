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


def test_healthz_hidden_from_non_loopback(client):
    response = client.get("/healthz", REMOTE_ADDR="203.0.113.7")
    assert response.status_code == 404


@pytest.mark.django_db
def test_healthz_allows_ipv6_loopback(client):
    response = client.get("/healthz", REMOTE_ADDR="::1")
    assert response.status_code == 200
