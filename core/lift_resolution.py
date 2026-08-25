"""Tracker-agnostic raw-exercise-name -> canonical-lift-name resolution.

Extracted from workout_imports.importers.strong.StrongImporter._resolve_lift,
which was the one place this six-stage chain existed -- every other importer
(Hevy, Liftosaur CSV) and live-sync service (liftosaur.services, wger.services)
only ever did a bare, single-stage alias lookup
(``alias_map.get(exercise.lower(), exercise)``), silently missing anything a
raw name needed equipment-qualifier handling or fuzzy matching to resolve.
Moving the chain here lets every caller share it instead of reimplementing a
subset.

The safety properties documented on the original ``_resolve_lift`` are load
bearing and preserved verbatim here:

* Stage 2 strips **only** a trailing "(Barbell)" qualifier
  (``_SAFE_TO_STRIP_QUALIFIERS``). Stripping is lossy: blindly stripping
  "(Dumbbell)"/"(Machine)"/etc and matching the bare canonical name risks
  silently attributing a set to the wrong equipment variant (the catalogue
  distinguishes some, e.g. Bench Press vs Chest Press). An unmapped/verbatim
  import is the safer failure mode than a wrong one.
* Stage 4 (reorder) transforms the *input* ("Base (Qualifier)" ->
  "Qualifier Base") and looks the result up in the same 1:1 canonical map
  stage 5 uses. It deliberately does NOT precompute a second "reversed key"
  map over the catalogue -- that would create two keys per canonical name,
  which could collide with each other and (via dict construction) silently
  keep whichever name happened to be written last.
* The per-import dedupe sets for "reordered"/"fuzzy"/"unmapped" are
  independent of each other, so a single raw name is only ever warned about
  once per status, not once per status per occurrence.
"""

import logging
import re
from dataclasses import dataclass

# Matches a trailing "(Equipment)" qualifier a tracker appends to its exercise
# names, e.g. "Pendlay Row (Barbell)" -> base "Pendlay Row", qualifier
# "Barbell". Only a single, non-nested parenthetical at the very end counts.
_TRAILING_QUALIFIER_RE = re.compile(r"^(?P<base>.+?)\s*\((?P<qualifier>[^()]+)\)$")

# Equipment qualifiers safe to strip and match against the bare canonical
# lift name -- see the module docstring for why this is a narrow allowlist,
# not a blanket strip.
_SAFE_TO_STRIP_QUALIFIERS = frozenset({"barbell"})

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_lift_name(name: str) -> str:
    """Fold a lift name for case/punctuation-insensitive comparison.

    Lowercases and collapses any run of non-alphanumeric characters (spaces,
    hyphens, apostrophes) to a single space, so "Chin Up", "Chin-up", and
    "chin  up" all normalize identically. Deliberately does not drop
    parenthesized content -- normalizing "Bench Press (Dumbbell)" keeps the
    "dumbbell" token, so it never accidentally collapses onto the bare
    "Bench Press" canonical name.
    """
    return _NON_ALNUM_RE.sub(" ", name.lower()).strip()


def normalize_lift_name_strict(name: str) -> str:
    """Fold a lift name for the separator-free catch-all match (stage 5).

    Lowercases and removes non-alphanumerics entirely rather than collapsing
    them to a space, so "Chinup", "Chin-up", and "Chin Up" all normalize to
    "chinup". Strictly looser than ``normalize_lift_name`` -- every name it
    equates, that function already equates too -- so it's only ever tried
    after that one has already failed. Still operates on the whole raw
    string with no equipment-qualifier stripping, so "Bench Press
    (Dumbbell)" normalizes to "benchpressdumbbell", not "benchpress" --
    the equipment-collapse guard holds here too.
    """
    return _NON_ALNUM_RE.sub("", name.lower())


@dataclass(frozen=True)
class LiftNameMaps:
    """The four lookup maps the resolution chain needs, precomputed once.

    Building these costs one query for the alias pairs and one for the
    canonical catalogue, however many raw names get resolved against them --
    the read-side equivalent of batching writes, avoiding one alias SELECT
    per row/set the way a naive per-call lookup would.
    """

    alias: dict[str, str]
    alias_strict: dict[str, str]
    canonical: dict[str, str]
    canonical_strict: dict[str, str]

    @classmethod
    def build(
        cls,
        alias_pairs,
        canonical_names,
    ) -> "LiftNameMaps":
        """Build all four maps from raw ``(from_name, to_name)`` alias pairs
        and an iterable of canonical lift names.

        Callers own the DB access (which alias table/source, which canonical
        catalogue) -- this stays pure so it can be unit tested without a
        database.
        """
        pairs = list(alias_pairs)
        names = list(canonical_names)
        return cls(
            alias={from_name.lower(): to_name for from_name, to_name in pairs},
            alias_strict={
                normalize_lift_name_strict(from_name): to_name
                for from_name, to_name in pairs
            },
            canonical={normalize_lift_name(name): name for name in names},
            canonical_strict={normalize_lift_name_strict(name): name for name in names},
        )


def build_lift_alias_maps(source: str, canonical_names) -> LiftNameMaps:
    """Convenience wrapper: fetch this source's alias pairs from ``core.LiftAlias``
    and build ``LiftNameMaps`` against them and the given canonical catalogue.

    ``canonical_names`` stays a caller-supplied iterable (rather than this
    module querying ``liftosaur.models.Lift`` itself) so core never imports
    from an app that depends on core -- see ``core.models.LiftAliasSource``
    for the same reasoning applied to the source discriminator.
    """
    from core.models import LiftAlias

    alias_pairs = LiftAlias.objects.filter(source=source).values_list(
        "from_name", "to_name"
    )
    return LiftNameMaps.build(alias_pairs, canonical_names)


def resolve_lift_name(exercise: str, maps: LiftNameMaps) -> tuple[str, str]:
    """Resolve a raw exercise name to a canonical lift name.

    Tries, in order:

    1. An explicit alias entry (case-insensitive) -- always wins when
       present, since it's a human-confirmed mapping.
    2. If the name ends in a "(Barbell)" qualifier, strip it and retry both
       the alias map and the canonical catalogue against the base name.
       Only "(Barbell)" is stripped this way; see
       ``_SAFE_TO_STRIP_QUALIFIERS`` for why other equipment isn't.
    3. A case-insensitive, punctuation-tolerant match of the whole raw name
       against the canonical catalogue (catches "Chin Up" vs "Chin-up").
    4. A qualifier-reorder match: for "Base (Qualifier)", build
       "Qualifier Base" and look it up (separator-free) against the
       canonical catalogue -- some trackers suffix equipment while the
       catalogue prefixes it, e.g. "Crunch (Cable)" -> "Cable Crunch".
       Unlike stage 2, this has no equipment allowlist and applies to any
       qualifier: reordering is information-preserving (the equipment token
       stays in the key), so it can only ever match a canonical name that
       names that exact equipment. This transforms the *input* and looks it
       up in the same ``canonical_strict`` map stage 5 uses -- it
       deliberately does not precompute a second "reordered key" map over
       the catalogue (see module docstring).
    5. A separator-free catch-all: lowercase with every non-alphanumeric
       character removed entirely, matched against both the canonical
       catalogue and the alias-map keys under the same folding (catches
       "Chinup", "TBar Row"). Strictly looser than stage 3, so it only ever
       fires once stages 3-4 have already missed.

    Returns ``(lift_name, status)``, where ``status`` is ``"matched"`` for
    stages 1-3, ``"reordered"`` when only stage 4 found it, ``"fuzzy"`` when
    only stage 5 found it (both are hits the caller should surface -- loose
    matching stood in for a proper alias or canonical entry), or
    ``"unmapped"`` when nothing did, in which case ``lift_name`` is the
    original ``exercise`` string, unchanged.
    """
    hit = maps.alias.get(exercise.lower())
    if hit:
        return hit, "matched"

    qualifier_match = _TRAILING_QUALIFIER_RE.match(exercise)
    if qualifier_match:
        qualifier = qualifier_match.group("qualifier").strip().lower()
        if qualifier in _SAFE_TO_STRIP_QUALIFIERS:
            base = qualifier_match.group("base").strip()
            hit = maps.alias.get(base.lower())
            if hit:
                return hit, "matched"
            hit = maps.canonical.get(normalize_lift_name(base))
            if hit:
                return hit, "matched"

    hit = maps.canonical.get(normalize_lift_name(exercise))
    if hit:
        return hit, "matched"

    if qualifier_match:
        base = qualifier_match.group("base").strip()
        qualifier_raw = qualifier_match.group("qualifier").strip()
        reordered = f"{qualifier_raw} {base}"
        hit = maps.canonical_strict.get(normalize_lift_name_strict(reordered))
        if hit:
            return hit, "reordered"

    strict_key = normalize_lift_name_strict(exercise)
    hit = maps.canonical_strict.get(strict_key)
    if hit:
        return hit, "fuzzy"
    hit = maps.alias_strict.get(strict_key)
    if hit:
        return hit, "fuzzy"

    return exercise, "unmapped"


class LiftNameResolver:
    """Per-import wrapper around ``resolve_lift_name``: resolves a name and
    warns (deduped, once per distinct raw name and status) when a fallback
    stage was needed.

    Construct one per CSV upload / sync pull -- the dedupe sets are instance
    state, mirroring the local ``warned_exercises`` / ``fuzzy_matched_exercises``
    /``reordered_matched_exercises`` sets the original Strong importer kept in
    ``parse()``. Not meant to be shared across concurrent imports.

    ``source_label`` and ``logger`` are what let a warning say which tracker
    it came from while still being attributed to the *caller's* logger
    (e.g. ``workout_imports.importers.hevy``), not a generic resolver logger.
    """

    def __init__(
        self,
        maps: LiftNameMaps,
        *,
        source_label: str,
        logger: logging.Logger,
    ):
        self._maps = maps
        self._source_label = source_label
        self._logger = logger
        self._warned_unmapped: set[str] = set()
        self._warned_reordered: set[str] = set()
        self._warned_fuzzy: set[str] = set()

    def resolve(self, exercise: str) -> str:
        lift, status = resolve_lift_name(exercise, self._maps)
        if status == "reordered" and exercise not in self._warned_reordered:
            self._warned_reordered.add(exercise)
            self._logger.warning(
                "%s: exercise %r only resolved to canonical lift %r by "
                "reordering its equipment qualifier to the front (stage 4); "
                "consider adding an explicit alias or double-checking this "
                "correspondence",
                self._source_label,
                exercise,
                lift,
            )
        elif status == "fuzzy" and exercise not in self._warned_fuzzy:
            self._warned_fuzzy.add(exercise)
            self._logger.warning(
                "%s: exercise %r only resolved to canonical lift %r via "
                "separator-insensitive fallback matching (stage 5); "
                "consider adding an explicit alias or double-checking this "
                "correspondence",
                self._source_label,
                exercise,
                lift,
            )
        elif status == "unmapped" and exercise not in self._warned_unmapped:
            self._warned_unmapped.add(exercise)
            self._logger.warning(
                "%s: exercise %r did not match any known alias or canonical "
                "lift; importing/pooling sets under this name verbatim",
                self._source_label,
                exercise,
            )
        return lift
