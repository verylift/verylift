"""Tests for the Create Challenge wizard (TASK-15, rebuilt TASK-247, TASK-248,
issue #85).

The wizard is a session-tracked, one-step-per-request flow (mirrors
accounts.onboarding_view): a fixed name -> dates -> mode -> lifts. Every
challenge is CUSTOM (TASK-248) -- the owner no longer picks a
chart-generation standard at all; each participant builds their own goal
chart at join, via goal_setup_view. Issue #85 inserted the "mode" step
(Classic vs Rep Target) right after dates; TASK-272 removed the old fourth
(invitees) step, so submitting "lifts" is still what creates the Challenge,
always with fixed history_window/plate_unit/smallest_plate, and it hands off
to the share screen carrying the challenge's invite link.
"""

import re
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeLift, ChallengeParticipant
from challenges.services import create_challenge
from challenges.tests.factories import ChallengeFactory  # noqa: F401
from liftosaur.models import Lift
from notifications.models import Notification


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def creator(db):
    return UserFactory()


@pytest.fixture
def other_user(db):
    return UserFactory()


@pytest.fixture
def authed_client(creator):
    c = Client()
    c.force_login(creator)
    return c


@pytest.fixture
def known_lifts(db):
    """A few lifts from the seeded catalogue (conftest.py autouse-seeds all 139)."""
    return {
        name: Lift.objects.get(name=name)
        for name in ("Bench Press", "Back Squat", "Deadlift")
    }


def _complete_wizard(
    client,
    *,
    name="Spring Showdown",
    start_date="2027-03-01",
    end_date="2027-06-01",
    mode=Challenge.Mode.CLASSIC,
    lift_pks,
):
    """Walk all four wizard steps and return the final (lifts) response."""
    url = reverse("challenges:create")
    client.post(url, {"name": name})
    client.post(url, {"start_date": start_date, "end_date": end_date})
    client.post(url, {"mode": mode})
    return client.post(url, {"lifts": list(lift_pks)})


class TestCreateChallengeViewAuth:
    def test_unauthenticated_request_redirects_to_login(self, client, db):
        url = reverse("challenges:create")
        response = client.get(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_unauthenticated_post_redirects_to_login(self, client, db):
        url = reverse("challenges:create")
        response = client.post(url, {"name": "x"})
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_authenticated_get_returns_200(self, authed_client):
        url = reverse("challenges:create")
        response = authed_client.get(url)
        assert response.status_code == 200

    def test_template_used(self, authed_client):
        url = reverse("challenges:create")
        response = authed_client.get(url)
        assert "challenges/create.html" in [t.name for t in response.templates]


class TestWizardShellCentering:
    """Every step of the create flow renders inside base/wizard.html's centered
    column (TASK-281). Asserts on the `data-wizard-shell` hook rather than the
    Tailwind class string so a width tweak doesn't break the test."""

    def test_every_step_renders_the_centered_shell(self, authed_client, known_lifts):
        url = reverse("challenges:create")

        name_step = authed_client.get(url)
        assert b"data-wizard-shell" in name_step.content
        assert b"mx-auto" in name_step.content

        authed_client.post(url, {"name": "Spring Showdown"})
        dates_step = authed_client.get(url)
        assert dates_step.context["step"] == "dates"
        assert b"data-wizard-shell" in dates_step.content

        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        mode_step = authed_client.get(url)
        assert mode_step.context["step"] == "mode"
        assert b"data-wizard-shell" in mode_step.content

        authed_client.post(url, {"mode": Challenge.Mode.CLASSIC})
        lifts_step = authed_client.get(url)
        assert lifts_step.context["step"] == "lifts"
        assert b"data-wizard-shell" in lifts_step.content

    def test_share_screen_renders_the_centered_shell(self, authed_client, known_lifts):
        response = _complete_wizard(
            authed_client,
            lift_pks=[known_lifts["Bench Press"].pk],
        )
        share = authed_client.get(response["Location"])
        assert "challenges/share.html" in [t.name for t in share.templates]
        assert b"data-wizard-shell" in share.content


class TestWizardStepProgression:
    def test_fresh_get_starts_on_name_step(self, authed_client):
        response = authed_client.get(reverse("challenges:create"))
        assert response.context["step"] == "name"
        assert response.context["step_number"] == 1
        # Fixed 4-step wizard: name, dates, mode, lifts (TASK-248, TASK-272,
        # issue #85).
        assert response.context["total_steps"] == 4

    def test_name_step_advances_to_dates(self, authed_client):
        url = reverse("challenges:create")
        response = authed_client.post(url, {"name": "Spring Showdown"})
        assert response.status_code == 302
        assert response["Location"] == url
        followed = authed_client.get(url)
        assert followed.context["step"] == "dates"
        assert followed.context["step_number"] == 2

    def test_missing_name_rejected_and_stays_on_name_step(self, authed_client):
        url = reverse("challenges:create")
        response = authed_client.post(url, {"name": ""})
        assert response.status_code == 200
        assert response.context["step"] == "name"
        assert Challenge.objects.count() == 0

    def test_dates_step_advances_to_mode(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        response = authed_client.post(
            url, {"start_date": "2027-03-01", "end_date": "2027-06-01"}
        )
        assert response.status_code == 302
        followed = authed_client.get(url)
        assert followed.context["step"] == "mode"
        assert followed.context["step_number"] == 3
        assert followed.context["total_steps"] == 4

    def test_mode_step_advances_to_lifts(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        response = authed_client.post(url, {"mode": Challenge.Mode.CLASSIC})
        assert response.status_code == 302
        followed = authed_client.get(url)
        assert followed.context["step"] == "lifts"
        assert followed.context["step_number"] == 4
        assert followed.context["total_steps"] == 4

    def test_invalid_mode_rejected_and_stays_on_mode_step(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        response = authed_client.post(url, {"mode": "not-a-real-mode"})
        assert response.status_code == 200
        assert response.context["step"] == "mode"
        assert Challenge.objects.count() == 0

    def test_end_date_before_start_date_rejected(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        response = authed_client.post(
            url, {"start_date": "2027-06-01", "end_date": "2027-03-01"}
        )
        assert response.status_code == 200
        assert response.context["step"] == "dates"
        assert b"End date must be after start date" in response.content
        assert Challenge.objects.count() == 0

    def test_end_date_equal_to_start_date_rejected(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        response = authed_client.post(
            url, {"start_date": "2027-03-01", "end_date": "2027-03-01"}
        )
        assert response.status_code == 200
        assert response.context["step"] == "dates"
        assert Challenge.objects.count() == 0

    def test_lifts_step_creates_challenge_and_leaves_the_wizard(
        self, authed_client, creator, known_lifts
    ):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        authed_client.post(url, {"mode": Challenge.Mode.CLASSIC})
        response = authed_client.post(
            url, {"lifts": [str(known_lifts["Bench Press"].pk)]}
        )
        assert response.status_code == 302
        comp = Challenge.objects.get(creator=creator)
        assert response["Location"] == reverse("challenges:share", args=[comp.pk])

    def test_empty_lift_list_rejected(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        authed_client.post(url, {"mode": Challenge.Mode.CLASSIC})
        response = authed_client.post(url, {"lifts": []})
        assert response.status_code == 200
        assert response.context["step"] == "lifts"
        assert b"Select at least one lift" in response.content
        assert Challenge.objects.count() == 0

    def test_unknown_lift_id_rejected(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        authed_client.post(url, {"mode": Challenge.Mode.CLASSIC})
        response = authed_client.post(
            url, {"lifts": ["00000000-0000-0000-0000-000000000000"]}
        )
        assert response.status_code == 200
        assert Challenge.objects.count() == 0

    def test_back_returns_to_previous_step_with_data_prefilled(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        response = authed_client.get(url, {"back": "1"})
        assert response.context["step"] == "dates"
        content = response.content.decode()
        assert 'value="2027-03-01"' in content
        assert 'value="2027-06-01"' in content

    def test_back_at_first_step_stays_on_first_step(self, authed_client):
        url = reverse("challenges:create")
        response = authed_client.get(url, {"back": "1"})
        assert response.context["step"] == "name"

    def test_cancel_clears_session_and_redirects_to_dashboard(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        response = authed_client.get(url, {"cancel": "1"})
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
        # Session cleared: a fresh visit starts back on the name step.
        followed = authed_client.get(url)
        assert followed.context["step"] == "name"

    def test_back_from_mode_returns_to_dates_prefilled(self, authed_client):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        assert authed_client.get(url).context["step"] == "mode"

        response = authed_client.get(url, {"back": "1"})

        assert response.context["step"] == "dates"
        content = response.content.decode()
        assert 'value="2027-03-01"' in content
        assert Challenge.objects.count() == 0

    def test_back_from_lifts_returns_to_mode_prefilled(self, authed_client):
        """Lifts is the last step now (TASK-272, issue #85)."""
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        authed_client.post(url, {"mode": Challenge.Mode.REP_TARGET})
        assert authed_client.get(url).context["step"] == "lifts"

        response = authed_client.get(url, {"back": "1"})

        assert response.context["step"] == "mode"
        assert response.context["form"].initial["mode"] == Challenge.Mode.REP_TARGET
        assert Challenge.objects.count() == 0


class TestLiftPickerStep:
    def _lift_rows(self, content):
        return re.findall(r"<label[^>]*data-lift-row[^>]*>", content)

    def _goto_lifts_step(self, authed_client, mode=Challenge.Mode.CLASSIC):
        url = reverse("challenges:create")
        authed_client.post(url, {"name": "Spring Showdown"})
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        authed_client.post(url, {"mode": mode})
        return authed_client.get(url)

    def test_renders_every_seeded_lift(self, authed_client):
        content = self._goto_lifts_step(authed_client).content.decode()
        rows = self._lift_rows(content)
        assert len(rows) == 139

    def test_nothing_pre_checked_on_fresh_form(self, authed_client):
        """No silent default preset (issue #85 follow-up) -- an owner picks a
        tab and hits its "Select all", or checks lifts by hand."""
        content = self._goto_lifts_step(authed_client).content.decode()
        checked = re.findall(r'<input[^>]*name="lifts"[^>]*checked[^>]*>', content)
        assert len(checked) == 0

    def test_search_input_rendered(self, authed_client):
        content = self._goto_lifts_step(authed_client).content.decode()
        assert "data-lift-search" in content

    def test_clear_all_button_rendered(self, authed_client):
        content = self._goto_lifts_step(authed_client).content.decode()
        assert "data-clear-lifts" in content

    def test_popular_group_heading_rendered(self, authed_client):
        content = self._goto_lifts_step(authed_client).content.decode()
        assert "Popular" in content
        assert "All Lifts" in content


class TestNoAdvancedFields:
    """AC#4: the units/rounding/visibility advanced drawer no longer exists."""

    def test_wizard_never_renders_removed_fields(self, authed_client, known_lifts):
        url = reverse("challenges:create")
        steps_content = []
        steps_content.append(authed_client.get(url).content.decode())  # name
        authed_client.post(url, {"name": "Spring Showdown"})
        steps_content.append(authed_client.get(url).content.decode())  # dates
        authed_client.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        steps_content.append(authed_client.get(url).content.decode())  # mode
        authed_client.post(url, {"mode": Challenge.Mode.CLASSIC})
        steps_content.append(authed_client.get(url).content.decode())  # lifts

        for content in steps_content:
            assert 'name="visibility"' not in content
            assert 'name="plate_unit"' not in content
            assert 'name="smallest_plate"' not in content
            assert 'name="bodyweight_tolerance"' not in content
            assert 'name="history_window"' not in content


class TestFullWizardCreatesChallenge:
    def test_creates_challenge_with_correct_name_and_dates(
        self, authed_client, creator, known_lifts
    ):
        response = _complete_wizard(
            authed_client, lift_pks=[known_lifts["Bench Press"].pk]
        )
        assert response.status_code == 302
        comp = Challenge.objects.get(creator=creator)
        assert comp.name == "Spring Showdown"
        assert comp.start_date.isoformat() == "2027-03-01"
        assert comp.end_date.isoformat() == "2027-06-01"

    def test_redirects_to_share_screen(self, authed_client, creator, known_lifts):
        """AC#4: the new owner lands on the share screen, not straight in goal
        setup, so the challenge's brand-new invite link is actually seen."""
        response = _complete_wizard(
            authed_client, lift_pks=[known_lifts["Bench Press"].pk]
        )
        comp = Challenge.objects.get(creator=creator)
        assert response["Location"] == reverse("challenges:share", args=[comp.pk])

    def test_challenge_has_draft_status(self, authed_client, creator, known_lifts):
        _complete_wizard(authed_client, lift_pks=[known_lifts["Bench Press"].pk])
        comp = Challenge.objects.get(creator=creator)
        assert comp.status == Challenge.Status.DRAFT

    def test_configured_lifts_persisted(self, authed_client, creator, known_lifts):
        _complete_wizard(
            authed_client,
            lift_pks=[known_lifts["Bench Press"].pk, known_lifts["Back Squat"].pk],
        )
        comp = Challenge.objects.get(creator=creator)
        names = set(
            ChallengeLift.objects.filter(challenge=comp).values_list("name", flat=True)
        )
        assert names == {"Bench Press", "Back Squat"}

    def test_mode_defaults_to_classic(self, authed_client, creator, known_lifts):
        _complete_wizard(authed_client, lift_pks=[known_lifts["Bench Press"].pk])
        comp = Challenge.objects.get(creator=creator)
        assert comp.mode == Challenge.Mode.CLASSIC

    def test_rep_target_mode_persisted_when_chosen(
        self, authed_client, creator, known_lifts
    ):
        _complete_wizard(
            authed_client,
            mode=Challenge.Mode.REP_TARGET,
            lift_pks=[known_lifts["Bench Press"].pk],
        )
        comp = Challenge.objects.get(creator=creator)
        assert comp.mode == Challenge.Mode.REP_TARGET

    def test_history_window_defaults_to_from_start(
        self, authed_client, creator, known_lifts
    ):
        _complete_wizard(authed_client, lift_pks=[known_lifts["Bench Press"].pk])
        comp = Challenge.objects.get(creator=creator)
        assert comp.history_window == Challenge.HistoryWindow.FROM_START

    def test_default_equipment_config_persisted(
        self, authed_client, creator, known_lifts
    ):
        _complete_wizard(authed_client, lift_pks=[known_lifts["Bench Press"].pk])
        comp = Challenge.objects.get(creator=creator)
        assert comp.plate_unit == Challenge.PlateUnit.LB
        assert comp.smallest_plate == Decimal("1.25")

    def test_creates_creator_participant(self, authed_client, creator, known_lifts):
        _complete_wizard(authed_client, lift_pks=[known_lifts["Bench Press"].pk])
        comp = Challenge.objects.get(creator=creator)
        participant = ChallengeParticipant.objects.get(challenge=comp, user=creator)
        assert participant.invite_status == ChallengeParticipant.InviteStatus.ACCEPTED
        assert participant.joined_at is not None

    def test_creates_only_the_creator_as_participant(
        self, authed_client, creator, known_lifts
    ):
        """TASK-272: nothing but the creator is added at creation — everyone
        else joins through the invite link."""
        _complete_wizard(authed_client, lift_pks=[known_lifts["Bench Press"].pk])
        comp = Challenge.objects.get(creator=creator)
        assert comp.participants.count() == 1
        assert Notification.objects.filter(challenge=comp).count() == 0

    def test_creates_a_live_invite_link(self, authed_client, creator, known_lifts):
        _complete_wizard(authed_client, lift_pks=[known_lifts["Bench Press"].pk])
        comp = Challenge.objects.get(creator=creator)
        assert comp.invite_links.filter(revoked_at__isnull=True).count() == 1

    def test_session_cleared_after_creation(self, authed_client, creator, known_lifts):
        _complete_wizard(authed_client, lift_pks=[known_lifts["Bench Press"].pk])
        # A fresh visit after finishing starts a brand-new wizard, not stuck
        # on the completed run's final step.
        response = authed_client.get(reverse("challenges:create"))
        assert response.context["step"] == "name"


class TestCreateChallengeAtomicity:
    def _cleaned_data(self):
        return {
            "name": "Rollback Cup",
            "start_date": "2027-03-01",
            "end_date": "2027-06-01",
            "custom_lift_names": ["Bench Press"],
            "history_window": Challenge.HistoryWindow.FROM_START,
            "plate_unit": Challenge.PlateUnit.LB,
            "smallest_plate_kg": Decimal("1.25"),
        }

    def test_mid_creation_failure_persists_nothing(self, monkeypatch, creator, db):
        """The invite link is minted last, so a failure there must still roll
        back the challenge, its lifts and the creator's participant row."""

        def boom(*args, **kwargs):
            raise RuntimeError("simulated mid-creation failure")

        monkeypatch.setattr("challenges.services.regenerate_invite_link", boom)

        with pytest.raises(RuntimeError):
            create_challenge(creator, self._cleaned_data())

        assert Challenge.objects.count() == 0
        assert ChallengeParticipant.objects.count() == 0
        assert ChallengeLift.objects.count() == 0
