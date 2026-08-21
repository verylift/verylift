"""Forms for the challenges app."""

import logging
from decimal import Decimal

from django import forms
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from accounts.units import from_display_weight
from challenges.custom_goals import (
    custom_goal_is_complete,
    parse_custom_goal_grid,
    parse_custom_goal_json,
    unknown_lift_error,
    validate_rep_max_monotonicity,
)
from challenges.goal_builders import default_goal_name
from challenges.lift_presets import CALISTHENICS_LIFT_NAMES, CLASSIC_LIFT_NAMES
from challenges.models import Challenge, CustomGoal
from challenges.services import challenge_display_end_of_day
from fitnessvolt import services as fitnessvolt_services
from fitnessvolt.models import FitnessVoltStandardCache
from liftosaur.models import Lift

logger = logging.getLogger(__name__)

_INPUT_CSS = (
    "w-full bg-surface-card text-content-body border border-line rounded"
    " px-3 py-2 text-sm focus:outline-none focus:border-accent"
)

# Native <input type="date"> needs a 16px+ font size on mobile. Below that
# threshold, focusing the input triggers iOS Safari's auto-zoom-on-focus
# behavior, and the resulting zoom/scroll-into-view animation can swallow the
# tap that was meant to open the native date picker (the picker never opens,
# looking like a dead input). text-sm (14px) is fine on desktop, so only
# override below the md breakpoint.
_DATE_INPUT_CSS = (
    "w-full bg-surface-card text-content-body border border-line rounded"
    " px-3 py-2 text-base md:text-sm focus:outline-none focus:border-accent"
)


class CreateChallengeNameForm(forms.Form):
    """Step 1 of the create wizard: the challenge's name."""

    name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": _INPUT_CSS,
                "placeholder": _("Challenge name"),
                "autofocus": True,
            }
        ),
    )


class CreateChallengeDatesForm(forms.Form):
    """Step 2 of the create wizard: start/end dates.

    Scoring can apply retroactively from the start date once the challenge is
    live — the copy explaining that lives in the template, not here.
    """

    start_date = forms.DateField(
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "class": _DATE_INPUT_CSS,
            }
        )
    )
    end_date = forms.DateField(
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "class": _DATE_INPUT_CSS,
            }
        )
    )

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        if start_date and end_date and end_date <= start_date:
            raise forms.ValidationError(gettext("End date must be after start date."))
        return cleaned_data


class CreateChallengeModeForm(forms.Form):
    """Step 3 of the create wizard: Classic vs Rep Target (issue #85).

    Whole-challenge, not mixable per-lift, and locked once chosen -- the
    template explains each mode with a short blurb; this form just validates
    the choice against the two real Challenge.Mode values.
    """

    mode = forms.ChoiceField(
        choices=Challenge.Mode.choices,
        widget=forms.RadioSelect(),
        initial=Challenge.Mode.CLASSIC,
    )


class CreateChallengeLiftsForm(forms.Form):
    """Step 4 of the create wizard: which lifts count.

    One flat list of every lift in the canonical liftosaur.Lift catalogue
    (picking from it, rather than free text, guarantees every chosen name
    matches a participant's actual Liftosaur history). The template pins
    CLASSIC_LIFT_NAMES ("Popular") above the rest, which render alphabetically
    (Lift.Meta.ordering). This list is the owner's call for the whole
    challenge — every participant's goal chart covers exactly these lifts.
    """

    lifts = forms.ModelMultipleChoiceField(
        queryset=Lift.objects.none(),
        widget=forms.CheckboxSelectMultiple(),
        error_messages={"required": _("Select at least one lift.")},
    )

    def __init__(self, *args, mode=Challenge.Mode.CLASSIC, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lifts"].queryset = Lift.objects.order_by("name")
        # Pre-check a default preset on the unbound (fresh) form so the
        # default selection works without JS -- Rep Target defaults to the
        # Calisthenics group (issue #85: it's the mode calisthenics
        # challenges will typically reach for), Classic keeps its existing
        # default. A bound form must keep whatever the POST carried, so only
        # seed initial when unbound.
        if not self.is_bound:
            default_names = (
                CALISTHENICS_LIFT_NAMES
                if mode == Challenge.Mode.REP_TARGET
                else CLASSIC_LIFT_NAMES
            )
            self.fields["lifts"].initial = list(
                Lift.objects.filter(name__in=default_names).values_list("pk", flat=True)
            )
        # Exposed for the template so the "Popular"/"Calisthenics" groups can
        # test membership.
        self.classic_lift_names = CLASSIC_LIFT_NAMES
        self.calisthenics_lift_names = CALISTHENICS_LIFT_NAMES


class GoalMethodForm(forms.Form):
    """Join goal-setup wizard, step 1: which of the four methods to use.

    Permanence is warned about in the template, not here — charts are locked
    once saved (AC#4). ``standards_available`` drops the "strength standards"
    choice (both here and, via the identical shared check, in the template)
    when FitnessVolt isn't configured or no population has a warmed
    snapshot — offering it otherwise leads to an empty population picker
    with no way to finish. This also rejects a POST of method=standards from
    a stale tab that loaded the form before the flag was true, or a
    tampered request, since ChoiceField validates against the narrowed
    choices either way.
    """

    method = forms.ChoiceField(
        choices=CustomGoal.SourceMethod.choices,
        widget=forms.RadioSelect(),
    )

    def __init__(self, *args, standards_available=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not standards_available:
            self.fields["method"].choices = [
                choice
                for choice in CustomGoal.SourceMethod.choices
                if choice[0] != CustomGoal.SourceMethod.STANDARDS
            ]


class GoalInputsForm(forms.Form):
    """Join goal-setup wizard, step 2 (standards or history, always -- see
    below): the ephemeral, goal-setup-only inputs (TASK-248 plan §1c).

    Bodyweight is collected here for both the standards and history methods —
    neither is ever written to ``User`` or tracked over time; sex/population/
    tier are standards-only. ``User.Sex`` no longer exists, so sex choices come
    from the FitnessVolt seam (``FitnessVoltStandardCache.Sex``) rather than a
    shared vocabulary. ``rounding_increment`` applies to BOTH methods (UAT
    feedback: raw Epley/uplift math produces "crazy numbers" for history, and
    FitnessVolt's own percentile interpolation across weight classes lands on
    arbitrary values for standards too) -- this is *why* the inputs step is
    now always part of the history path, not just when a bodyweight-added
    lift is configured: a rounding choice is needed for every history-derived
    suggestion, not only those. ``uplift_percent`` is history-only -- it has
    no standards equivalent (there is nothing to "stretch" in a published
    table).
    """

    bodyweight = forms.FloatField(
        required=False,
        error_messages={"invalid": _("Enter a bodyweight greater than zero.")},
    )
    sex = forms.CharField(required=False)
    population = forms.CharField(required=False)
    tier = forms.CharField(required=False)
    rounding_increment = forms.CharField(required=False)
    uplift_percent = forms.FloatField(
        required=False,
        error_messages={"invalid": _("Enter a stretch percentage (e.g. 10).")},
    )

    def __init__(self, *args, method, needs_bodyweight=True, unit="kg", **kwargs):
        super().__init__(*args, **kwargs)
        self.method = method
        self.needs_bodyweight = needs_bodyweight
        # No per-field unit override anywhere in this form (bodyweight,
        # rounding) -- always the account's own unit_preference. A separate
        # "which unit is THIS number in" selector per field is exactly the
        # trap UAT reported: it can silently drift from what the user
        # actually has set as their preference, entering e.g. "80" meaning
        # lb while a stale/defaulted dropdown still reads "kg".
        self.unit = unit
        self.is_standards = method == CustomGoal.SourceMethod.STANDARDS
        self.is_history = method == CustomGoal.SourceMethod.HISTORY
        self.needs_rounding = self.is_standards or self.is_history
        self.sex_choices = (
            list(FitnessVoltStandardCache.Sex.choices) if self.is_standards else []
        )
        self.population_choices = (
            [
                (population.value, population.label)
                for population in FitnessVoltStandardCache.Population
                if fitnessvolt_services.current_snapshot_version(population.value)
            ]
            if self.is_standards
            else []
        )
        self.tier_choices = (
            list(fitnessvolt_services.TIER_TARGET_PERCENTILE)
            if self.is_standards
            else []
        )
        self.default_uplift_percent = settings.CHALLENGES_GOAL_SUGGESTION_UPLIFT * 100
        # Preset tokens are "none", or "<unit>:<amount in that unit>" -- kept
        # as a display-unit amount (not a pre-converted kg value) so the
        # dropdown shows clean, familiar numbers (2.5 lb, not its ugly kg
        # equivalent); clean() keeps this amount+unit pair intact rather than
        # pre-converting to kg -- goal_builders._round_to_increment rounds in
        # this exact unit and converts only the final value, because
        # pre-converting a clean "5 lb" into kg (from_display_weight's own
        # 0.01 kg precision) drifts away from clean 5 lb multiples as the
        # multiplier grows (UAT: "204.3 lb" from a "5 lb" choice). Choices are
        # unit-scoped (not both units offered at once) to match the
        # account's own unit_preference, the same single source of truth
        # bodyweight now uses too -- no separate unit selector to drift out
        # of sync with it.
        if self.needs_rounding and unit == "lb":
            self.rounding_choices = [
                ("none", _("No rounding (exact)")),
                ("lb:2.5", "2.5 lb"),
                ("lb:5", "5 lb"),
                ("lb:10", "10 lb"),
            ]
            self.default_rounding = "lb:5"
        elif self.needs_rounding:
            self.rounding_choices = [
                ("none", _("No rounding (exact)")),
                ("kg:1", "1 kg"),
                ("kg:2.5", "2.5 kg"),
                ("kg:5", "5 kg"),
            ]
            self.default_rounding = "kg:2.5"
        else:
            self.rounding_choices = []
            self.default_rounding = "none"

    def clean(self):
        cleaned_data = super().clean()
        bodyweight_kg = None
        if self.needs_bodyweight:
            bodyweight = cleaned_data.get("bodyweight")
            if not bodyweight or bodyweight <= 0:
                self.add_error(
                    "bodyweight", gettext("Enter a bodyweight greater than zero.")
                )
            else:
                bodyweight_kg = from_display_weight(Decimal(str(bodyweight)), self.unit)
        cleaned_data["bodyweight_kg"] = bodyweight_kg

        if self.is_history:
            uplift_percent = cleaned_data.get("uplift_percent")
            if uplift_percent is None:
                uplift_percent = self.default_uplift_percent
            cleaned_data["uplift"] = Decimal(str(uplift_percent)) / Decimal("100")
        else:
            cleaned_data["uplift"] = None

        if self.needs_rounding:
            valid_tokens = {token for token, _label in self.rounding_choices}
            raw = cleaned_data.get("rounding_increment") or self.default_rounding
            if raw not in valid_tokens:
                self.add_error(
                    "rounding_increment", gettext("Select a rounding increment.")
                )
                raw = self.default_rounding
            # Written back (not just read) so a caller can stash this exact
            # token and echo it as next GET's initial.
            cleaned_data["rounding_increment"] = raw
            if raw == "none":
                cleaned_data["rounding_amount"] = None
                cleaned_data["rounding_unit"] = None
            else:
                round_unit, amount = raw.split(":")
                # Kept as a Decimal amount + its own unit, NOT converted to
                # kg here -- from_display_weight's 0.01 kg precision would
                # turn a clean "5 lb" into an approximate 2.27 kg, and
                # rounding to multiples of THAT drifts away from clean 5 lb
                # multiples as the multiplier grows (UAT: "204.3 lb" from a
                # "5 lb" choice). goal_builders._round_to_increment rounds
                # in this unit directly and converts only the final value.
                cleaned_data["rounding_amount"] = Decimal(amount)
                cleaned_data["rounding_unit"] = round_unit
        else:
            cleaned_data["rounding_amount"] = None
            cleaned_data["rounding_unit"] = None

        if self.is_standards:
            sex = cleaned_data.get("sex")
            if sex not in {value for value, _label in self.sex_choices}:
                self.add_error("sex", gettext("Select your sex."))

            population = cleaned_data.get("population")
            if population not in {value for value, _label in self.population_choices}:
                self.add_error("population", gettext("Select a population."))
            else:
                snapshot_version = fitnessvolt_services.current_snapshot_version(
                    population
                )
                if snapshot_version is None:
                    # Should not happen — __init__ only offers populations with
                    # a warmed snapshot — but fail closed rather than pin nothing.
                    logger.error(
                        "FitnessVolt population %s offered in goal-setup wizard "
                        "but has no warmed snapshot at submit time",
                        population,
                    )
                    self.add_error(
                        "population",
                        gettext("Strength standards are not available right now."),
                    )
                cleaned_data["snapshot_version"] = snapshot_version

            tier = cleaned_data.get("tier")
            if tier not in self.tier_choices:
                self.add_error("tier", gettext("Select a tier."))

        return cleaned_data


class HistoryWindowForm(forms.Form):
    """Owner-facing control (Settings page) for the point-eligible window.

    Moved out of creation (TASK-247): the default is fixed to FROM_START at
    creation time and can only be changed here, mid-challenge.
    """

    history_window = forms.ChoiceField(
        choices=Challenge.HistoryWindow.choices,
        widget=forms.RadioSelect(),
    )


class RenameChallengeForm(forms.Form):
    name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": _INPUT_CSS,
                "placeholder": _("Challenge name"),
                "autofocus": True,
            }
        ),
    )


class InviteLinkOptionsForm(forms.Form):
    """Owner-facing overrides at invite-link generation time (Settings/Share).

    Three independent expiry states: blank ``expires_at`` with the "Never
    expires" checkbox unchecked falls back to
    challenges.services._default_invite_link_expiry (the challenge's own
    end_date); a filled ``expires_at`` uses that value verbatim (subject to
    the validation below); and the "Never expires" checkbox, when checked,
    forces ``expires_at`` to None regardless of whatever was typed in the
    date field -- the checkbox always wins, with no validation error even if
    a date was also entered. ``max_uses`` is a fully independent field:
    blank means unlimited uses (the section's clear button blanks the field
    for exactly this). ``0`` is rejected rather than silently treated as
    unlimited -- a cap of zero admits no one, which is never what a
    submitter meant, so 1 is the lowest accepted explicit value.

    Pass ``challenge`` so a custom ``expires_at`` can be bounded to the
    challenge's own end_date -- a link that outlives the competition it's
    for doesn't make sense, and the plain-default expiry already caps there
    (services._default_invite_link_expiry), so a custom value shouldn't be
    able to exceed it either. The bound does not apply when "Never expires"
    is checked -- that's the whole point of the checkbox.
    """

    expires_at = forms.DateTimeField(
        required=False,
        # Native <input type="datetime-local"> submits "%Y-%m-%dT%H:%M" (no
        # seconds), which isn't among Django's default DATETIME_INPUT_FORMATS
        # (those use a space separator, not "T") -- add it explicitly rather
        # than relying on the defaults.
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"],
        widget=forms.DateTimeInput(
            format="%Y-%m-%dT%H:%M",
            attrs={"type": "datetime-local", "class": _DATE_INPUT_CSS},
        ),
    )
    never_expires = forms.BooleanField(
        required=False,
        label=_("Never expires"),
        widget=forms.CheckboxInput(attrs={"class": "h-4 w-4 rounded border-line"}),
    )
    max_uses = forms.IntegerField(
        required=False,
        min_value=1,
        widget=forms.NumberInput(
            attrs={
                "class": _INPUT_CSS,
                "placeholder": _("Unlimited"),
                "min": "1",
            }
        ),
    )

    def __init__(self, *args, challenge=None, **kwargs):
        self.challenge = challenge
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("never_expires"):
            cleaned_data["expires_at"] = None
            self.errors.pop("expires_at", None)
        return cleaned_data

    def clean_expires_at(self):
        expires_at = self.cleaned_data.get("expires_at")
        if expires_at is None:
            return None
        # The datetime-local widget submits a naive local value; interpret it
        # in the current (server) timezone rather than leaving it naive, since
        # this becomes ChallengeInviteLink.expires_at (an aware DateTimeField).
        if timezone.is_naive(expires_at):
            expires_at = timezone.make_aware(expires_at)
        if expires_at <= timezone.now():
            raise forms.ValidationError(gettext("Expiry must be in the future."))
        if self.challenge is not None:
            challenge_end = challenge_display_end_of_day(
                self.challenge, self.challenge.end_date
            )
            if expires_at > challenge_end:
                raise forms.ValidationError(
                    gettext("Expiry can't be after the challenge ends.")
                )
        return expires_at


class CustomGoalForm(forms.Form):
    """Goal name plus a target table sourced from JSON paste OR manual grid.

    Which path is parsed is keyed off ``method``: JSON parses
    ``targets_json``, every other method (standards/history/manual) parses
    the grid fields — the two are peer top-level goal-setup methods, not a
    toggle within one screen (TASK-306). The name field is only
    rendered/required for the grid path — a JSON submission carries its own
    required top-level "name" key instead, so ``self.name`` is resolved from
    whichever path was used. Either way the parsed ``{lift: {rep: kg}}`` table
    is exposed on ``self.targets`` so a failed submit can re-render the grid
    prefilled with whatever parsed cleanly, and completeness (every configured
    lift × reps 1–10) plus rep-max monotonicity are enforced before the form
    validates.

    JSON-pasted lift names not configured for the challenge (TASK-314) are an
    acknowledge-and-proceed case rather than an always-fatal one: when they're
    the ONLY problem with the payload, the form re-renders invalid but exposes
    them on ``self.unknown_lifts`` so the view/template can offer an explicit
    "ignore and continue" checkbox (``acknowledge_unknown_lifts``) instead of
    silently dropping them or permanently blocking the save. Checking that box
    and resubmitting saves using whatever targets DID parse. Any OTHER error
    (malformed JSON, bad unit, non-numeric weight, incompleteness) still
    blocks the whole payload regardless of the checkbox — it only ever waives
    the unknown-lift complaint, never a real one.
    """

    name = forms.CharField(
        label=_("Goal name"),
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={"class": _INPUT_CSS, "placeholder": _("e.g. Spring targets")}
        ),
    )
    targets_json = forms.CharField(
        label=_("Targets JSON"),
        required=False,
        widget=forms.Textarea(attrs={"class": _INPUT_CSS, "rows": 6}),
    )
    acknowledge_unknown_lifts = forms.BooleanField(required=False)

    def __init__(
        self,
        *args,
        challenge=None,
        unit="kg",
        method=CustomGoal.SourceMethod.CUSTOM,
        method_kwargs=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.challenge = challenge
        self.unit = unit
        self.method = method
        self.method_kwargs = method_kwargs or {}
        self.targets: dict = {}
        self.name = ""
        self.unknown_lifts: list[str] = []

    def clean(self):
        cleaned_data = super().clean()
        # Only the JSON method offers JSON-paste in the UI: pasting JSON
        # over a standards/history-prefilled (or manual-entry) grid would let
        # the saved targets diverge from what source_detail claims produced
        # them, so a targets_json value is ignored (grid path used instead)
        # for any other method rather than trusted, closing that off even
        # against a stale tab or a hand-crafted request.
        payload = (
            (cleaned_data.get("targets_json") or "").strip()
            if self.method == CustomGoal.SourceMethod.JSON
            else ""
        )
        unknown_lifts: list[str] = []
        if payload:
            name, targets, errors, unknown_lifts = parse_custom_goal_json(
                payload, self.challenge, self.unit
            )
        else:
            targets, errors = parse_custom_goal_grid(
                self.data, self.challenge, self.unit
            )
            name = (cleaned_data.get("name") or "").strip()
            if not name:
                # A goal name is never demanded (TASK-248 plan §4): a sensible
                # default is used, editable but not required.
                name = default_goal_name(self.method, **self.method_kwargs)
        self.targets = targets
        self.name = name
        self.unknown_lifts = unknown_lifts

        other_errors = (
            errors
            + custom_goal_is_complete(targets, self.challenge)
            + validate_rep_max_monotonicity(targets, self.challenge)
        )
        if unknown_lifts and not other_errors:
            # TASK-314: unknown lift names are the ONLY problem — an
            # acknowledge-and-proceed case, not an always-fatal one. Unrecognized
            # targets are already excluded from ``targets`` by the parser, so
            # once acknowledged there's nothing left to do here; the save just
            # proceeds with whatever parsed cleanly.
            if not cleaned_data.get("acknowledge_unknown_lifts"):
                self.add_error(
                    None,
                    gettext(
                        "Some lift names in your JSON weren't recognized. Review "
                        "them below and confirm to continue, or edit the JSON."
                    ),
                )
        else:
            # Any other error type still blocks the whole payload even if the
            # acknowledgment checkbox is set (AC#4) -- surface the unknown-lift
            # complaints too in that case, since checking the box doesn't mean
            # the user has seen them yet.
            for lift_name in unknown_lifts:
                self.add_error(None, unknown_lift_error(lift_name))
            for error in other_errors:
                self.add_error(None, error)
        return cleaned_data

    def banner_errors(self) -> list[str]:
        """All errors flattened for the template's single error banner —
        field errors prefixed with the field label, non-field errors as-is."""
        errors = list(self.non_field_errors())
        for bound in self:
            errors.extend(f"{bound.label}: {e}" for e in bound.errors)
        return errors
