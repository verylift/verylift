"""Forms for the challenges app."""

import logging
from decimal import Decimal

from django import forms
from django.conf import settings
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from accounts.units import from_display_weight
from challenges.custom_goals import (
    custom_goal_is_complete,
    parse_custom_goal_grid,
    parse_custom_goal_json,
)
from challenges.goal_builders import default_goal_name
from challenges.lift_presets import CLASSIC_LIFT_NAMES
from challenges.models import Challenge, CustomGoal
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


class CreateChallengeLiftsForm(forms.Form):
    """Step 3 of the create wizard: which lifts count.

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lifts"].queryset = Lift.objects.order_by("name")
        # Pre-check the Classics preset on the unbound (fresh) form so the
        # default selection works without JS. A bound form must keep whatever
        # the POST carried, so only seed initial when unbound.
        if not self.is_bound:
            self.fields["lifts"].initial = list(
                Lift.objects.filter(name__in=CLASSIC_LIFT_NAMES).values_list(
                    "pk", flat=True
                )
            )
        # Exposed for the template so the "Popular" group can test membership.
        self.classic_lift_names = CLASSIC_LIFT_NAMES


class GoalMethodForm(forms.Form):
    """Join goal-setup wizard, step 1: which of the three methods to use.

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


class CustomGoalForm(forms.Form):
    """Goal name plus a target table sourced from JSON paste OR manual grid.

    The two input paths are equally first-class: a non-empty JSON textarea is
    parsed as the source, otherwise the manual grid fields are. The name field
    is only rendered/required for the grid path — a JSON submission carries its
    own required top-level "name" key instead, so ``self.name`` is resolved from
    whichever path was used. Either way the parsed ``{lift: {rep: kg}}`` table
    is exposed on ``self.targets`` so a failed submit can re-render the grid
    prefilled with whatever parsed cleanly, and completeness (every configured
    lift × reps 1–10) is enforced before the form validates.
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

    def clean(self):
        cleaned_data = super().clean()
        # Only the CUSTOM method offers JSON-paste in the UI: pasting JSON
        # over a standards/history-prefilled grid would let the saved
        # targets diverge from what source_detail claims produced them, so
        # a targets_json value is ignored (grid path used instead) for any
        # other method rather than trusted, closing that off even against a
        # stale tab or a hand-crafted request.
        payload = (
            (cleaned_data.get("targets_json") or "").strip()
            if self.method == CustomGoal.SourceMethod.CUSTOM
            else ""
        )
        if payload:
            name, targets, errors = parse_custom_goal_json(
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
        for error in errors + custom_goal_is_complete(targets, self.challenge):
            self.add_error(None, error)
        return cleaned_data

    def banner_errors(self) -> list[str]:
        """All errors flattened for the template's single error banner —
        field errors prefixed with the field label, non-field errors as-is."""
        errors = list(self.non_field_errors())
        for bound in self:
            errors.extend(f"{bound.label}: {e}" for e in bound.errors)
        return errors
