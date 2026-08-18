import uuid
from datetime import UTC, datetime, time
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Challenge(models.Model):
    class PlateUnit(models.TextChoices):
        LB = "lb", _("Pounds (lb)")
        KG = "kg", _("Kilograms (kg)")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ACTIVE = "active", _("Active")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")

    # The end-state statuses a challenge can never leave: it takes no further
    # goal changes, invites, or moderation actions once it reaches either.
    TERMINAL_STATUSES = (Status.COMPLETED, Status.CANCELLED)

    class HistoryWindow(models.TextChoices):
        FROM_JOIN = "from_join", _("From join date")
        FROM_START = "from_start", _("From challenge start")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_challenges",
    )
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    history_window = models.CharField(
        max_length=20,
        choices=HistoryWindow.choices,
        default=HistoryWindow.FROM_JOIN,
    )
    # Canonical unit the creator configured this challenge's equipment in. It
    # governs only how smallest_plate INPUT is interpreted at creation time —
    # it never affects display. The viewing user's personal unit_preference
    # (accounts app, TASK-83) always governs displayed units.
    plate_unit = models.CharField(
        max_length=2,
        choices=PlateUnit.choices,
        default=PlateUnit.LB,
    )
    # Smallest plate available at the venue, stored in kg (this codebase's
    # canonical weight-storage convention). The minimum loadable barbell change
    # is one plate per side, so displayed weights snap to a 2 x smallest_plate
    # grid. Default 1.25 kg (a 2.5 kg loadable increment).
    smallest_plate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("1.25")
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "challenges_challenge"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def is_terminal(self) -> bool:
        """True when the challenge is in an end-state (COMPLETED/CANCELLED)."""
        return self.status in self.TERMINAL_STATUSES

    def window_start_for(self, participant):
        """Effective point-eligible window start for a participant.

        Returns a timezone-aware datetime, or None when in from_join mode and
        the participant has not yet joined. In from_start mode the window opens
        at the challenge's start date for every participant.
        """
        if self.history_window == self.HistoryWindow.FROM_START:
            return datetime.combine(self.start_date, time.min, tzinfo=UTC)
        return participant.joined_at


class ChallengeInviteLink(models.Model):
    """A single shareable, time-limited join link for a challenge.

    Anyone holding the token can join (or register-then-join, per
    challenges.views.invite_link_view) — no per-invitee targeting, unlike the
    legacy user-search invite lifecycle this sits alongside. At most one row
    per challenge is ever "live" (revoked_at is null and not yet expired); the
    partial unique constraint below enforces that at the DB level. It is
    satisfiable only because challenges.services.regenerate_invite_link always
    revokes the incumbent live row (including a merely-expired-but-unrevoked
    one) inside the same transaction before creating a new one — do not drop
    that revoke when touching this constraint.

    Rows are never hard-deleted -- this app's auditability policy limits hard
    delete to credential material, and a bearer join token is a capability,
    not an account credential: revoking makes a token inert while preserving
    the audit trail behind AC#3 (acquisition source).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name="invite_links",
    )
    token = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_invite_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    # Null = unlimited uses. Owner-facing override at generation time
    # (challenges.forms.InviteLinkOptionsForm); independent of expires_at.
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    # Always starts at 0 for a fresh row -- never carried over from a revoked
    # incumbent (challenges.services.regenerate_invite_link), and incremented
    # only on an actual join (challenges.services.record_invite_link_use).
    use_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "challenges_challengeinvitelink"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["challenge"],
                condition=models.Q(revoked_at__isnull=True),
                name="challengeinvitelink_one_live_per_challenge",
            ),
        ]

    def __str__(self):
        return f"Invite link for {self.challenge}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_exhausted(self) -> bool:
        return self.max_uses is not None and self.use_count >= self.max_uses

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None and not self.is_expired and not self.is_exhausted


class ChallengeLift(models.Model):
    """A single lift a CUSTOM challenge is scored on.

    Custom challenges have no built-in or FitnessVolt source to enumerate
    their lifts, so the creator names them explicitly at creation time. This is
    the authoritative "configured lifts" list the standards seam
    (challenges.standards.covered_lift_names) reads, and the set every
    participant's CustomGoal must cover in full.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name="custom_lifts",
    )
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "challenges_challengelift"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["challenge", "name"],
                name="challengelift_unique_name_per_challenge",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.challenge})"


class ChallengeParticipant(models.Model):
    class InviteStatus(models.TextChoices):
        # Only ACCEPTED is ever written now (TASK-272 removed the user-search
        # invite lifecycle): it is the load-bearing "is a real member"
        # predicate threaded through dashboard_view,
        # _require_challenge_member, _notify_user_joined, get_co_participants,
        # close_challenge, _participants_section_context and scoring. INVITED
        # and DECLINED are retained for legacy rows only — nothing produces
        # them any more. Collapsing the field away is separate future cleanup.
        INVITED = "invited", _("Invited")
        ACCEPTED = "accepted", _("Accepted")
        DECLINED = "declined", _("Declined")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name="participants",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="challenge_participations",
    )
    # Every challenge's goal is a full per-lift rep-max target table bundled
    # under one named CustomGoal, pointed to here. PROTECT so an active goal
    # can never be orphaned.
    custom_goal = models.ForeignKey(
        "CustomGoal",
        on_delete=models.PROTECT,
        related_name="active_for",
        null=True,
        blank=True,
    )
    invite_status = models.CharField(
        max_length=20,
        choices=InviteStatus.choices,
        default=InviteStatus.INVITED,
    )
    joined_at = models.DateTimeField(null=True, blank=True)
    is_bailed = models.BooleanField(default=False)
    bailed_at = models.DateTimeField(null=True, blank=True)
    # Creator-initiated removal is implemented as bail-plus-flag: is_bailed and
    # bailed_at carry the scoring freeze so every existing bail filter applies
    # unchanged, and this flag only distinguishes a removal from a voluntary
    # leave — it blocks self-rejoin and drives the REMOVED notification.
    # Currently permanent for V1: nothing clears it (see ChallengeInviteLink
    # and TASK-249's Risks Q3).
    removed_by_creator = models.BooleanField(default=False)
    # Per-membership provenance: which invite link admitted this participant,
    # if any (older rows and non-link joins are null). The per-user half of
    # acquisition tracking is accounts.User.acquisition_source; this is the
    # per-join half (AC#3). SET_NULL rather than PROTECT — links are
    # challenge-scoped and cascade with the challenge, so a link's removal
    # must not block the challenge's own cascade.
    joined_via_link = models.ForeignKey(
        ChallengeInviteLink,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="joined_participants",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "challenges_challengeparticipant"
        unique_together = [("challenge", "user")]

    def __str__(self):
        return f"{self.user} in {self.challenge}"

    @property
    def has_goal_configured(self):
        """True when the participant has completed goal setup."""
        return self.custom_goal_id is not None


class CustomGoal(models.Model):
    """A participant's named bundle of per-lift rep-max targets for a CUSTOM
    challenge.

    One goal covers every lift the challenge is configured on, each with an
    explicit 1RM–10RM target table (CustomGoalTarget). A goal is only "usable"
    once every configured lift has all ten rep counts; save_custom_goal enforces
    that at write time so an active goal is complete by construction.

    created_at IS the lock timestamp: charts cannot be edited after joining
    (AC#4), so there is no separate locked_at to track.
    """

    class SourceMethod(models.TextChoices):
        STANDARDS = "standards", _("Strength standards")
        HISTORY = "history", _("Suggested from history")
        CUSTOM = "custom", _("Manual entry")
        JSON = "json", _("Paste JSON")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    participant = models.ForeignKey(
        ChallengeParticipant,
        on_delete=models.CASCADE,
        related_name="custom_goals",
    )
    name = models.CharField(max_length=100)
    source_method = models.CharField(
        max_length=20,
        choices=SourceMethod.choices,
        default=SourceMethod.CUSTOM,
    )
    # A one-time record of the inputs a suggested chart was computed from.
    # This is the ONLY place a bodyweight or a sex value persists anywhere in
    # the product — deliberately JSON, not a column, so nothing can filter,
    # join, or aggregate on it. Never write it for HISTORY or CUSTOM goals;
    # never read it from scoring; never copy it onto User; never add a second
    # sample. Shapes, exactly:
    #   STANDARDS -> {"population": str, "snapshot_version": str, "tier": str,
    #                 "sex": "M"|"F", "bodyweight_kg": str}
    #   HISTORY   -> {"uplift": float, "lookback_days": int}
    #   CUSTOM    -> {}
    # bodyweight_kg is a decimal STRING: this JSONField uses Django's default
    # encoder, which cannot serialise Decimal, and a float would drift the
    # very number this field exists to pin. See TASK-248 plan §4 and the
    # privacy policy's per-goal provenance disclosure, which must stay in
    # sync with this shape.
    source_detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "challenges_customgoal"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["participant", "name"],
                name="customgoal_unique_name_per_participant",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.participant})"


class CustomGoalTarget(models.Model):
    """One rep-max target cell: the weight (kg) a participant is aiming to lift
    for a given lift at a given rep count.

    target_weight is stored in kg (the codebase's canonical convention). For
    bodyweight-added lifts (Pull-up/Chin-up/Dip) it is the ADDED weight relative
    to bodyweight — 0 means bodyweight-only, negative means leverage-machine
    assisted — matching Liftosaur's own recorded convention; scoring compares it
    directly against the recorded LiftHistory weight, which is also added
    weight for these lifts (scoring.services._GoalTargets.targets_for — no
    bodyweight arithmetic anywhere). For all other lifts it is the absolute
    target load. Positivity for non-bodyweight-added lifts is enforced in
    challenges.custom_goals (there is no DB-level positivity constraint because
    it is conditional on the lift's bodyweight-added quality, which is not
    expressible in SQL here — lift is a name, not an FK).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    goal = models.ForeignKey(
        CustomGoal,
        on_delete=models.CASCADE,
        related_name="targets",
    )
    lift = models.CharField(max_length=100)
    rep_count = models.PositiveSmallIntegerField()
    target_weight = models.DecimalField(max_digits=7, decimal_places=2)

    class Meta:
        db_table = "challenges_customgoaltarget"
        ordering = ["lift", "rep_count"]
        constraints = [
            models.UniqueConstraint(
                fields=["goal", "lift", "rep_count"],
                name="customgoaltarget_unique_lift_rep_per_goal",
            ),
            models.CheckConstraint(
                condition=models.Q(rep_count__gte=1, rep_count__lte=10),
                name="customgoaltarget_rep_count_1_to_10",
            ),
        ]

    def __str__(self):
        return f"{self.lift} {self.rep_count}RM @ {self.target_weight}kg"
