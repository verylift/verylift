import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from policies.forms import PolicyConsentForm
from policies.models import Policy, PolicyConsent
from policies.services import pending_versions_for, record_consent

logger = logging.getLogger(__name__)


def policy_list_view(request):
    policies = Policy.objects.prefetch_related("versions")
    return render(request, "policies/policy_list.html", {"policies": policies})


def policy_detail_view(request, slug):
    policy = get_object_or_404(Policy, slug=slug)
    return render(
        request,
        "policies/policy_detail.html",
        {
            "policy": policy,
            "active_version": policy.versions.filter(is_active=True).first(),
            "all_versions": policy.versions.order_by("-effective_date"),
        },
    )


def _safe_next_url(request, next_url):
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        return next_url
    return "/"


@login_required
def consent_view(request):
    if request.method == "POST":
        form = PolicyConsentForm(request.POST)
        pending = pending_versions_for(request.user)
        if form.is_valid():
            record_consent(
                request.user, request, pending, PolicyConsent.Method.RE_CONSENT
            )
            return redirect(_safe_next_url(request, request.POST.get("next", "")))
        return render(
            request,
            "policies/consent_form.html",
            {
                "form": form,
                "pending_versions": pending,
                "next": request.POST.get("next", ""),
            },
        )

    pending = pending_versions_for(request.user)
    if not pending.exists():
        return redirect(_safe_next_url(request, request.GET.get("next", "")))
    return render(
        request,
        "policies/consent_form.html",
        {
            "form": PolicyConsentForm(),
            "pending_versions": pending,
            "next": request.GET.get("next", ""),
        },
    )
