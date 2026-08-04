from django.test import RequestFactory

from core.http import is_htmx


def test_is_htmx_true_when_header_present():
    request = RequestFactory().get("/", HTTP_HX_REQUEST="true")
    assert is_htmx(request) is True


def test_is_htmx_false_when_header_absent():
    request = RequestFactory().get("/")
    assert is_htmx(request) is False
