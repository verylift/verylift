"""Tests for the dashboard home view (TASK-26)."""

import re
from html.parser import HTMLParser
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from PIL import Image

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
    CustomGoalFactory,
)
from scoring.services import build_points_over_time
from scoring.tests.factories import PointEarnEventFactory


def _tiny_png(name="avatar.png"):
    buffer = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def client(user):
    c = Client()
    c.force_login(user)
    return c


def _accepted(user, challenge):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )


class TestLandingPage:
    def test_anonymous_gets_landing_page(self, db):
        response = Client().get(reverse("challenges:landing"))
        assert response.status_code == 200
        assert "landing.html" in [t.name for t in response.templates]

    def test_landing_page_content_and_links(self, db):
        content = Client().get(reverse("challenges:landing")).content.decode()
        assert "Challenge your friends. Score fairly." in content
        assert reverse("accounts:register") in content
        assert reverse("accounts:login") in content
        assert reverse("terms") in content
        assert reverse("privacy") in content

    def test_authenticated_user_redirected_to_dashboard(self, client):
        response = client.get(reverse("challenges:landing"))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")


class TestDashboardAuth:
    def test_anonymous_redirected_to_login(self, db):
        response = Client().get(reverse("challenges:dashboard"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response["Location"]

    def test_renders_for_authenticated_user(self, client):
        response = client.get(reverse("challenges:dashboard"))
        assert response.status_code == 200
        assert "dashboard.html" in [t.name for t in response.templates]


class TestSectionPartitioning:
    def test_legacy_invited_row_in_no_section(self, client, user):
        """TASK-272: nothing creates INVITED rows any more and there is no
        invited bucket — a leftover row shows nowhere."""
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        response = client.get(reverse("challenges:dashboard"))
        assert response.context["active_cards"] == []
        assert response.context["completed_cards"] == []

    def test_active_section_populated(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        response = client.get(reverse("challenges:dashboard"))
        cards = response.context["active_cards"]
        assert [c["challenge"] for c in cards] == [challenge]
        assert response.context["completed_cards"] == []

    def test_completed_challenge_in_completed_section(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        _accepted(user, challenge)
        response = client.get(reverse("challenges:dashboard"))
        cards = response.context["completed_cards"]
        assert [c["challenge"] for c in cards] == [challenge]
        assert response.context["active_cards"] == []

    def test_voluntary_bail_hidden_from_every_section(self, client, user):
        """A departed participant always loses their card now. OPEN challenges
        used to keep it; that carve-out retired with open visibility itself
        (TASK-272)."""
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        participation = _accepted(user, challenge)
        participation.is_bailed = True
        participation.save()
        response = client.get(reverse("challenges:dashboard"))
        completed = [c["challenge"] for c in response.context["completed_cards"]]
        active = [c["challenge"] for c in response.context["active_cards"]]
        assert challenge not in completed
        assert challenge not in active

    def test_creator_removal_hidden_from_every_section(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        participation = _accepted(user, challenge)
        participation.is_bailed = True
        participation.removed_by_creator = True
        participation.save()
        response = client.get(reverse("challenges:dashboard"))
        completed = [c["challenge"] for c in response.context["completed_cards"]]
        active = [c["challenge"] for c in response.context["active_cards"]]
        assert challenge not in completed
        assert challenge not in active

    def test_bailed_from_completed_challenge_also_hidden(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        participation = _accepted(user, challenge)
        participation.is_bailed = True
        participation.save()
        response = client.get(reverse("challenges:dashboard"))
        completed = [c["challenge"] for c in response.context["completed_cards"]]
        assert challenge not in completed

    def test_declined_challenge_in_no_section(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.DECLINED,
        )
        response = client.get(reverse("challenges:dashboard"))
        assert response.context["active_cards"] == []
        assert response.context["completed_cards"] == []

    def test_other_users_participations_excluded(self, client, user):
        other = UserFactory()
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(other, challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert response.context["active_cards"] == []


class TestEmptyState:
    def test_all_sections_empty_shows_single_create_cta(self, client):
        response = client.get(reverse("challenges:dashboard"))
        assert response.context["active_cards"] == []
        assert response.context["completed_cards"] == []
        assert response.context["career"]["has_history"] is False
        body = response.content.decode()
        # The "Start a challenge" CTA only appears in the empty-state message
        # when the user has zero active challenges -- there is no persistent
        # CTA beside the Challenges title.
        assert body.count("Start a challenge") == 1
        assert reverse("challenges:create") in body
        assert "Challenges" in body
        assert "No active challenges right now." in body
        assert "Completed challenges" not in body

    def test_history_but_no_active_shows_cta_in_challenges_card(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        _accepted(user, challenge)
        response = client.get(reverse("challenges:dashboard"))
        body = response.content.decode()
        assert "Challenges" in body
        assert "No active challenges right now." in body
        assert body.count("Start a challenge") == 1


class TestCardStats:
    def test_points_total_sums_current_best_only(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            points_earned=6,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Bench",
            points_earned=4,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            points_earned=99,
            is_current_best=False,
        )
        response = client.get(reverse("challenges:dashboard"))
        card = response.context["active_cards"][0]
        assert card["total_points"] == 10

    def test_points_total_zero_when_no_events(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        response = client.get(reverse("challenges:dashboard"))
        card = response.context["active_cards"][0]
        assert card["total_points"] == 0
        assert card["rank"] is None

    def test_rank_reflects_leaderboard(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        leader = UserFactory()
        _accepted(leader, challenge)
        PointEarnEventFactory(
            user=leader,
            challenge=challenge,
            points_earned=50,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            points_earned=10,
            is_current_best=True,
        )
        response = client.get(reverse("challenges:dashboard"))
        card = response.context["active_cards"][0]
        assert card["rank"] == 2


class TestSetGoalCta:
    """Active-section cards surface a Set Goal CTA for participants who have not
    completed goal setup (TASK-162)."""

    def test_active_card_without_goal_shows_set_goal(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        content = client.get(reverse("challenges:dashboard")).content.decode()
        assert "Set Goal" in content
        assert reverse("challenges:goal-setup", args=[challenge.pk]) in content

    def test_active_card_with_goal_hides_set_goal(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        goal = CustomGoalFactory(participant=participant)
        participant.custom_goal = goal
        participant.save(update_fields=["custom_goal"])
        content = client.get(reverse("challenges:dashboard")).content.decode()
        assert "Set Goal" not in content

    def test_legacy_invited_row_does_not_show_set_goal(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        content = client.get(reverse("challenges:dashboard")).content.decode()
        assert "Set Goal" not in content

    def test_completed_card_without_goal_hides_set_goal(self, client, user):
        """A finished challenge never surfaces the CTA, even for an accepted
        participant who never set a goal — forcing goal setup (and the scoring it
        triggers) on an ended challenge would be wrong."""
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        response = client.get(reverse("challenges:dashboard"))
        content = response.content.decode()
        assert response.context["completed_cards"][0]["needs_goal"] is False
        assert "Set Goal" not in content


class TestLiftosaurKeyCta:
    """Keyless dashboard users see an explicit API-key-required CTA (TASK-250)."""

    def test_keyless_user_sees_add_key_banner(self, client):
        response = client.get(reverse("challenges:dashboard"))
        assert response.context["needs_liftosaur_key"] is True
        content = response.content.decode()
        assert "Connect your Liftosaur API key" in content
        assert reverse("accounts:settings") in content

    def test_keyed_user_does_not_see_add_key_banner(self, db):
        user = UserFactory(liftosaur_api_key="test-liftosaur-key")
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:dashboard"))
        assert response.context["needs_liftosaur_key"] is False
        assert "Connect your Liftosaur API key" not in response.content.decode()

    def test_keyless_user_still_sees_their_challenge_card(self, client, user):
        """A keyless user can still view a challenge they belong to — the key
        gate is at join time, not at read time (TASK-250)."""
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert response.status_code == 200
        cards = response.context["active_cards"]
        assert [c["challenge"] for c in cards] == [challenge]


class TestSidebarLogoLink:
    """The logo in both sidebar instances links to the dashboard (TASK-223)."""

    def test_logo_link_has_accessible_name(self, client):
        content = client.get(reverse("challenges:dashboard")).content.decode()
        assert 'aria-label="very lift, go to dashboard"' in content

    def test_logo_on_dashboard_renders_without_error(self, client):
        response = client.get(reverse("challenges:dashboard"))
        assert response.status_code == 200


class TestHeroCard:
    """The hero card shows avatar and career stats (TASK-246)."""

    def test_career_context_present(self, client):
        response = client.get(reverse("challenges:dashboard"))
        career = response.context["career"]
        assert career["challenges_played"] == 0
        assert career["has_history"] is False

    def test_stats_render_with_history(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        _accepted(user, challenge)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            points_earned=6,
            is_current_best=True,
        )
        body = client.get(reverse("challenges:dashboard")).content.decode()
        # The data-dependent bits only. The four stat labels are static copy
        # from the same template block, asserted here and in the no-history
        # test purely because they are easy to assert.
        assert "First scored point vs latest" in body
        assert "Squat" in body

    def test_avatar_shows_user_initials(self, client, user):
        user.display_name = "Taylor"
        user.save()
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert "TA" in body


class TestProfilePhotoCard:
    """Profile card goes full-bleed photo-as-background when a photo is set,
    with the name bottom-anchored on a content-width blurred sub-card (TASK-260)."""

    @pytest.fixture(autouse=True)
    def _media_root(self, settings, tmp_path):
        settings.MEDIA_ROOT = tmp_path

    def test_photo_renders_as_full_bleed_background(self, client, user):
        user.avatar = _tiny_png()
        user.save()
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert f'src="{user.avatar.url}"' in body
        assert "object-cover" in body
        assert "backdrop-blur-sm" in body

    def test_name_renders_on_photo_card_without_joined_date(self, client, user):
        user.display_name = "Taylor"
        user.avatar = _tiny_png()
        user.save()
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert "Taylor" in body
        assert "Joined" not in body

    def test_name_card_wraps_content_width_not_full_photo_width(self, client, user):
        user.avatar = _tiny_png()
        user.save()
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert "w-fit" in body
        assert "inset-x-0 bottom-0" not in body

    def test_no_photo_shows_initials_avatar_not_full_bleed_image(self, client, user):
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert "backdrop-blur-sm" not in body


class TestActiveChallengePanelLink:
    """Each active-challenge chart panel's title links to the detail page
    (TASK-246 originally; panels replaced static cards per later UAT
    feedback, but the click-through to the challenge itself is preserved)."""

    def test_panel_title_links_to_detail(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        body = client.get(reverse("challenges:dashboard")).content.decode()
        detail_url = reverse("challenges:detail", args=[challenge.pk])
        assert f'href="{detail_url}"' in body

    def test_set_goal_cta_present_alongside_title_link(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        body = client.get(reverse("challenges:dashboard")).content.decode()
        detail_url = reverse("challenges:detail", args=[challenge.pk])
        goal_setup_url = reverse("challenges:goal-setup", args=[challenge.pk])
        assert f'href="{detail_url}"' in body
        assert f'href="{goal_setup_url}"' in body


class TestCompletedDemoted:
    """Completed challenges collapse into a low-prominence disclosure (TASK-246)."""

    def test_completed_renders_in_details_disclosure(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        _accepted(user, challenge)
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert "<details" in body
        assert "Completed challenges" in body
        assert reverse("challenges:detail", args=[challenge.pk]) in body

    def test_no_disclosure_without_completed(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert "<details" not in body


class TestNotificationsSection:
    """Notifications render as a dashboard section (TASK-246)."""

    def test_section_renders_on_dashboard(self, client):
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert 'id="notifications-list-container"' in body
        assert "You have no notifications." in body

    def test_sidebar_has_no_notifications_link(self, client):
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert "/notifications/" not in body


class TestCoParticipantsCard:
    """People you've played with card (TASK-246, widened by TASK-279).

    TASK-279 moved the card into the dashboard's top-row grid and broadened
    the roster from active-only to active + completed challenges.
    """

    def test_no_shared_challenges_shows_empty_copy(self, client):
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == []
        body = response.content.decode()
        assert "People you've played with" in body
        assert "Once you join a challenge" in body

    def test_card_renders_in_top_row_exactly_once(self, client, user):
        other = UserFactory(display_name="Alex")
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        _accepted(other, challenge)
        body = client.get(reverse("challenges:dashboard")).content.decode()
        heading = "People you've played with"
        # Exactly once: the old standalone section must not linger alongside
        # the new grid cell.
        assert body.count(heading) == 1
        assert body.index(heading) < body.index('id="notifications-list-container"')

    def test_active_co_participant_listed(self, client, user):
        other = UserFactory(display_name="Alex")
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        _accepted(other, challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == [other]
        assert "Alex" in response.content.decode()

    def test_bailed_co_participant_excluded(self, client, user):
        other = UserFactory(display_name="Alex")
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=other,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == []

    def test_completed_challenge_co_participant_included(self, client, user):
        other = UserFactory(display_name="Alex")
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        _accepted(user, challenge)
        _accepted(other, challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == [other]

    def test_cancelled_challenge_co_participant_excluded(self, client, user):
        # CANCELLED is terminal but nobody played it, so `is_terminal` is the
        # wrong predicate here — only COMPLETED counts as "past".
        other = UserFactory(display_name="Alex")
        challenge = ChallengeFactory(status=Challenge.Status.CANCELLED)
        _accepted(user, challenge)
        _accepted(other, challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == []

    def test_draft_challenge_co_participant_excluded(self, client, user):
        other = UserFactory(display_name="Alex")
        challenge = ChallengeFactory(status=Challenge.Status.DRAFT)
        _accepted(user, challenge)
        _accepted(other, challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == []

    def test_viewer_bailed_from_completed_challenge_sees_no_co_participants(
        self, client, user
    ):
        other = UserFactory(display_name="Alex")
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        _accepted(other, challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == []

    def test_co_participant_bailed_from_completed_challenge_excluded(
        self, client, user
    ):
        other = UserFactory(display_name="Alex")
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        _accepted(user, challenge)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=other,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == []

    def test_inactive_co_participant_excluded(self, client, user):
        other = UserFactory(display_name="Alex", is_active=False)
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        _accepted(other, challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == []

    def test_dedupe_across_active_and_completed(self, client, user):
        other = UserFactory(display_name="Alex")
        active = ChallengeFactory(status=Challenge.Status.ACTIVE)
        done = ChallengeFactory(status=Challenge.Status.COMPLETED)
        for challenge in (active, done):
            _accepted(user, challenge)
            _accepted(other, challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == [other]

    def test_co_participant_deduped_across_shared_challenges(self, client, user):
        other = UserFactory(display_name="Alex")
        comp_a = ChallengeFactory(status=Challenge.Status.ACTIVE)
        comp_b = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, comp_a)
        _accepted(other, comp_a)
        _accepted(user, comp_b)
        _accepted(other, comp_b)
        response = client.get(reverse("challenges:dashboard"))
        assert list(response.context["co_participants"]) == [other]


class TestPointsOverTimeCarousel:
    """A Points Over Time chart panel per active challenge (TASK-260)."""

    def test_section_absent_with_no_active_challenges(self, client):
        response = client.get(reverse("challenges:dashboard"))
        assert response.context["active_cards"] == []
        body = response.content.decode()
        assert "potc-carousel" not in body

    def test_one_panel_per_active_challenge(self, client, user):
        challenge_a = ChallengeFactory(status=Challenge.Status.ACTIVE, name="Alpha")
        challenge_b = ChallengeFactory(status=Challenge.Status.ACTIVE, name="Beta")
        _accepted(user, challenge_a)
        _accepted(user, challenge_b)
        response = client.get(reverse("challenges:dashboard"))
        charts = response.context["active_cards"]
        assert {c["challenge"] for c in charts} == {challenge_a, challenge_b}
        body = response.content.decode()
        assert f'id="potc-chart-{challenge_a.pk}"' in body
        assert f'id="potc-chart-{challenge_b.pk}"' in body
        assert "Alpha" in body
        assert "Beta" in body

    def test_chart_data_matches_build_points_over_time(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            points_earned=6,
            is_current_best=True,
        )
        response = client.get(reverse("challenges:dashboard"))
        charts = response.context["active_cards"]
        assert len(charts) == 1
        assert charts[0]["chart_data"] == build_points_over_time(challenge)

    def test_invited_and_completed_challenges_excluded(self, client, user):
        invited_challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=invited_challenge,
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        completed_challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        _accepted(user, completed_challenge)
        response = client.get(reverse("challenges:dashboard"))
        assert response.context["active_cards"] == []

    def test_prev_next_controls_hidden_for_single_challenge(self, client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert "potc-carousel" in body
        assert 'id="potc-prev-btn"' not in body
        assert 'id="potc-next-btn"' not in body
        assert 'id="potc-dots"' not in body

    def test_prev_next_controls_shown_for_multiple_challenges(self, client, user):
        challenge_a = ChallengeFactory(status=Challenge.Status.ACTIVE)
        challenge_b = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge_a)
        _accepted(user, challenge_b)
        body = client.get(reverse("challenges:dashboard")).content.decode()
        assert 'id="potc-prev-btn"' in body
        assert 'id="potc-next-btn"' in body
        assert 'id="potc-dots"' in body

    def test_dots_are_not_desktop_only(self, client, user):
        """Dots show on desktop too, not just mobile (UAT feedback) — the
        dots container must not carry a `md:hidden` (or similar
        desktop-hiding) class."""
        challenge_a = ChallengeFactory(status=Challenge.Status.ACTIVE)
        challenge_b = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge_a)
        _accepted(user, challenge_b)
        body = client.get(reverse("challenges:dashboard")).content.decode()
        dots_div = re.search(r'<div id="potc-dots"[^>]*>', body).group(0)
        assert "hidden" not in dots_div

    def test_json_script_tags_nest_inside_their_own_panel(self, client, user):
        """Regression guard: each chart's json_script tag must be a
        descendant of its own `.snap-start` panel div, not a sibling of it
        directly under #potc-carousel. Previously it rendered as a sibling,
        which meant `#potc-carousel`'s direct children included both the
        panel divs *and* bare `<script>` tags. Client-side JS builds its
        panel list from `carousel.children` and calls
        `panel.querySelector("canvas")` / reads `panel.dataset.jsonId` on
        each one — for a `<script>` "panel" both are absent, throwing a
        TypeError that aborted the whole setup script (charts after the
        first never render, and the prev/next button listeners — wired up
        later in the same script — never get attached at all)."""
        challenge_a = ChallengeFactory(status=Challenge.Status.ACTIVE)
        challenge_b = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge_a)
        _accepted(user, challenge_b)
        body = client.get(reverse("challenges:dashboard")).content.decode()

        class _PanelChildParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.carousel_direct_children = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if self.stack and self.stack[-1].get("id") == "potc-carousel":
                    self.carousel_direct_children.append(tag)
                self.stack.append(attrs)
                if tag in ("br", "img", "input"):
                    self.stack.pop()

            def handle_endtag(self, tag):
                if self.stack:
                    self.stack.pop()

        parser = _PanelChildParser()
        parser.feed(body)
        # #potc-carousel's only direct children should be the two panel
        # divs — no stray <script> tags leaking in as siblings.
        assert parser.carousel_direct_children == ["div", "div"]
