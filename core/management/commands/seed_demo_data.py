import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from accounts.services import transcode_avatar_to_avif
from challenges.custom_goals import save_custom_goal
from challenges.goal_builders import standards_source_detail
from challenges.models import Challenge, ChallengeParticipant, CustomGoal
from challenges.services import (
    activate_draft_for_creator,
    close_challenge,
    create_challenge,
)
from core.models import LiftHistory
from scoring.domain.calculator import is_bodyweight_added_lift, tier_thresholds
from scoring.services import score_pooled_history

logger = logging.getLogger(__name__)

# (username, display_name) — a small fixed cast so seeding is idempotent and
# the same demo names show up run after run.
DEMO_USERS = [
    ("demo_alex", "Alex Rivera"),
    ("demo_sam", "Sam Okafor"),
    ("demo_jordan", "Jordan Wu"),
    ("demo_taylor", "Taylor Nguyen"),
    ("demo_morgan", "Morgan Silva"),
]

LIFTS = ["Back Squat", "Bench Press", "Deadlift"]
GOAL_TIER = "Intermediate"
DEMO_MULTIPLIER_BY_LIFT = {
    "Back Squat": Decimal("1.5"),
    "Bench Press": Decimal("1.0"),
    "Deadlift": Decimal("1.75"),
}
# Ephemeral, goal-setup-only bodyweight (TASK-248 plan §1c): used once per
# seeded participant to compute a standards-derived target ladder, exactly
# like the real wizard's standards method would, then discarded — never
# written to User or any other model. No account in this product ever has a
# bodyweight anywhere except a locked goal's one-time source_detail record.
DEMO_BODYWEIGHT_KG = Decimal("75.0")
AVATAR_COLORS = ["#1f9d63", "#2563eb", "#d97706", "#dc2626", "#7c3aed", "#0891b2"]


def _placeholder_avatar(initials, color):
    """A simple colored-circle-with-initials avatar, generated locally with
    Pillow (no external image service or network access required) and run
    through the same transcode_avatar_to_avif() path as a real upload, so
    seeded/UAT avatars are AVIF like production ones — not a PNG that never
    exercises the AVIF-serving path."""
    image = Image.new("RGB", (256, 256), color)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 96)
    except OSError:
        font = ImageFont.load_default()
    left, top, right, bottom = draw.textbbox((0, 0), initials, font=font)
    width, height = right - left, bottom - top
    draw.text(
        ((256 - width) / 2 - left, (256 - height) / 2 - top),
        initials,
        fill="white",
        font=font,
    )
    png_buffer = BytesIO()
    image.save(png_buffer, format="PNG")
    png_buffer.seek(0)
    avif_file = transcode_avatar_to_avif(png_buffer)
    if avif_file is not None:
        return avif_file
    png_buffer.seek(0)
    return ContentFile(png_buffer.getvalue(), name=f"{initials.lower()}.png")


class Command(BaseCommand):
    help = (
        "Seed demo users (with profile photos), challenges across every "
        "status, and lift histories for a review/UAT instance. Idempotent. "
        "Not part of seed_all — never run this against production."
    )

    def handle(self, *args, **options):
        users = self._seed_users()
        today = timezone.now().date()

        self._seed_challenge(
            users,
            "Demo Draft Challenge",
            Challenge.Status.DRAFT,
            start_date=today + timedelta(days=7),
            end_date=today + timedelta(days=37),
        )
        active = self._seed_challenge(
            users,
            "Demo Active Challenge",
            Challenge.Status.ACTIVE,
            start_date=today - timedelta(days=14),
            end_date=today + timedelta(days=16),
        )
        completed = self._seed_challenge(
            users,
            "Demo Completed Challenge",
            Challenge.Status.COMPLETED,
            start_date=today - timedelta(days=60),
            end_date=today - timedelta(days=30),
        )
        self._seed_challenge(
            users,
            "Demo Cancelled Challenge",
            Challenge.Status.CANCELLED,
            start_date=today - timedelta(days=10),
            end_date=today + timedelta(days=20),
        )

        for challenge in (active, completed):
            self._seed_lift_histories(users, challenge)

        # Close *after* lift histories are seeded and scored: close_challenge
        # locks the scoring ledger (process_scored_set becomes a no-op once
        # completed), so closing first would have scored against an empty
        # pool. Idempotent — no-ops if already completed.
        close_challenge(completed)

        logger.info("Demo data seeded: %s users", len(users))
        self.stdout.write(self.style.SUCCESS("Demo data seeded."))

    def _seed_users(self):
        User = get_user_model()
        users = []
        for index, (username, display_name) in enumerate(DEMO_USERS):
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "display_name": display_name,
                    "email": f"{username}@example.com",
                    "acquisition_source": User.AcquisitionSource.ADMIN,
                },
            )
            if created:
                user.set_unusable_password()
            if not user.avatar:
                initials = "".join(part[0] for part in display_name.split()[:2]).upper()
                color = AVATAR_COLORS[index % len(AVATAR_COLORS)]
                avatar_file = _placeholder_avatar(initials, color)
                # _placeholder_avatar names the file itself (uuid.avif on the
                # normal transcode path, initials.png on the no-codec
                # fallback), so the extension isn't known until after it
                # runs. A prior seed run against a since-reset database can
                # leave a same-named file behind; delete it first so this
                # save reuses the deterministic avatars/<username>.<ext> path
                # instead of storage auto-renaming to a new orphan.
                ext = avatar_file.name.rsplit(".", 1)[-1]
                avatar_name = f"{username}.{ext}"
                avatar_path = f"avatars/{avatar_name}"
                if default_storage.exists(avatar_path):
                    default_storage.delete(avatar_path)
                user.avatar.save(avatar_name, avatar_file, save=False)
            user.save()
            users.append(user)
        return users

    def _seed_challenge(self, users, name, target_status, *, start_date, end_date):
        creator = users[0]
        existing = Challenge.objects.filter(name=name, creator=creator).first()
        if existing:
            logger.info("Demo challenge %r already exists; skipping", name)
            return existing

        with transaction.atomic():
            challenge = create_challenge(
                creator,
                {
                    "name": name,
                    "start_date": start_date,
                    "end_date": end_date,
                    "history_window": Challenge.HistoryWindow.FROM_JOIN,
                    "plate_unit": Challenge.PlateUnit.KG,
                    "smallest_plate_kg": Decimal("1.25"),
                    "custom_lift_names": LIFTS,
                },
            )

        # Backdated to the challenge's own start, not "now" — a FROM_JOIN
        # scoring window anchored at today would exclude all of the historical
        # LiftHistory rows seeded below for already-started/completed demo
        # challenges. Applies to the creator too: create_challenge stamps
        # their own participant row with joined_at=now.
        backdated_join = timezone.make_aware(
            datetime.combine(challenge.start_date, datetime.min.time())
        )
        accepted = ChallengeParticipant.InviteStatus.ACCEPTED

        # create_challenge only ever adds the creator now (TASK-272 removed the
        # per-user invite batch), so the rest of the cast is written directly —
        # the same model-row-first approach _seed_goal uses. Driving the real
        # invite-link join view instead would need an HTTP request/session a
        # management command has no business faking.
        for user in users[1:]:
            ChallengeParticipant.objects.get_or_create(
                challenge=challenge,
                user=user,
                defaults={
                    "invite_status": accepted,
                    "joined_at": backdated_join,
                },
            )

        for participant in challenge.participants.all():
            changed_fields = []
            if participant.invite_status != accepted:
                participant.invite_status = accepted
                changed_fields.append("invite_status")
            if participant.joined_at != backdated_join:
                participant.joined_at = backdated_join
                changed_fields.append("joined_at")
            if changed_fields:
                participant.save(update_fields=changed_fields)
            if not participant.has_goal_configured:
                self._seed_goal(participant)

        if target_status in (Challenge.Status.ACTIVE, Challenge.Status.COMPLETED):
            # Both need to be ACTIVE first so lift histories can be scored
            # into them; the completed one is closed later, once its lift
            # histories exist (see handle() — close_challenge locks the
            # ledger, so closing here would score against an empty pool).
            activate_draft_for_creator(challenge, creator)
        elif target_status == Challenge.Status.CANCELLED:
            challenge.status = Challenge.Status.CANCELLED
            challenge.save(update_fields=["status"])
        # DRAFT needs no further transition — create_challenge already
        # leaves it there.

        logger.info("Seeded demo challenge %r (%s)", name, target_status)
        return challenge

    def _seed_goal(self, participant):
        """Materialise a demo participant's goal the same way the real
        standards method would: tier_thresholds computed from the ephemeral
        DEMO_BODYWEIGHT_KG, converted to added weight for bodyweight-added
        lifts, with a real source_detail record — so demo data exercises the
        genuine provenance shape a standards-derived chart carries rather than
        a `{}` special case (TASK-248 plan §4).
        """
        targets = {}
        for lift in LIFTS:
            multiplier = DEMO_MULTIPLIER_BY_LIFT.get(lift, Decimal("1.0"))
            thresholds = tier_thresholds(GOAL_TIER, multiplier, DEMO_BODYWEIGHT_KG)
            is_added = is_bodyweight_added_lift(lift)
            targets[lift] = {
                rm.reps: (rm.weight - DEMO_BODYWEIGHT_KG if is_added else rm.weight)
                for rm in thresholds.rep_maxes
            }
        sex = "M" if hash(participant.user_id) % 2 == 0 else "F"
        source_detail = standards_source_detail(
            population="gym",
            snapshot_version="demo-seed",
            tier=GOAL_TIER,
            sex=sex,
            bodyweight_kg=DEMO_BODYWEIGHT_KG,
        )
        save_custom_goal(
            participant,
            f"{GOAL_TIER} demo goal",
            targets,
            source_method=CustomGoal.SourceMethod.STANDARDS,
            source_detail=source_detail,
        )

    def _seed_lift_histories(self, users, challenge):
        """Direct LiftHistory rows (bypassing the real Liftosaur API, which
        demo users have no key for) scored through the real, local-only
        score_pooled_history so PointEarnEvents/leaderboards are genuine."""
        for user in users:
            base_weight = Decimal(random.uniform(40, 120)).quantize(Decimal("0.1"))
            for lift in LIFTS:
                for week in range(4):
                    performed_at = challenge.start_date + timedelta(days=week * 7)
                    if performed_at > timezone.now().date():
                        continue
                    weight = base_weight + Decimal(week * 2)
                    LiftHistory.objects.get_or_create(
                        user=user,
                        lift=lift,
                        performed_at=performed_at,
                        defaults={
                            "weight_kg": weight,
                            "reps": random.randint(1, 5),
                            "synced_at": timezone.now(),
                        },
                    )
            score_pooled_history(user=user, challenge=challenge)
