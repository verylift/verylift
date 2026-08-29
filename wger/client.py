"""HTTP client module for the Wger REST API.

This module is a thin wrapper around the official ``wger_api_client`` package
(generated from Wger's published OpenAPI schema) -- request construction,
query-param encoding, and response parsing are that package's responsibility,
not this module's. This module only owns what's specific to how the app calls
Wger: constructing a per-user authenticated client, translating between this
codebase's plain-value signatures and the generated client's typed
request/response objects, and mapping non-2xx responses onto ``WgerAPIError``.

Unlike Liftosaur, Wger is self-hostable: there is no single fixed base URL, so
the base URL is per-user, supplied alongside the API token.

Notes (see https://wger.readthedocs.io/en/latest/api/api.html and the
wger-project/wger source for background):

- Auth: ``Authorization: Token <token>`` header (the "permanent token" scheme;
  Wger's docs mark this deprecated in favor of short-lived JWTs obtained via
  ``/api/v2/token``, but it's the only scheme that doesn't require re-deriving
  a refresh flow, and it's still fully supported).
- Pagination: DRF LimitOffsetPagination -- ``limit``/``offset`` query params,
  response envelope ``{"count", "next", "previous", "results"}``.
- Workout logs: ``GET /api/v2/workoutlog/`` (``WorkoutLogViewSet``), scoped to
  the requesting user server-side. Supports a ``date__gte`` filter. Each entry
  references its exercise by a numeric ``exercise`` ID and its units by
  numeric ``weight_unit``/``repetitions_unit`` IDs -- Wger's exercise database
  is normalized, so there is no raw exercise-name string on the log entry
  itself.
- Exercise names: ``GET /api/v2/exerciseinfo/<id>/`` returns all
  ``translations`` for that exercise (no server-side language filter); the
  human-readable name lives at ``translations[i].name``.
- Body weight: ``GET /api/v2/weightentry/`` (``WeightEntryViewSet``) lists the
  requesting user's weigh-ins as ``{id, date, weight, user}``, where ``weight``
  is a decimal string with NO unit attached. The unit is a per-user setting
  read from ``GET /api/v2/userprofile/`` (``weight_unit``, "kg" or "lb"), which
  is why reading a body weight takes two calls rather than one.
- Weight/repetition units: ``GET /api/v2/setting-weightunit/`` and
  ``GET /api/v2/setting-repetitionunit/`` are small reference tables (a
  handful of rows, effectively unpaginated in practice) that resolve the
  numeric unit IDs referenced above to names (and, for repetition units,
  a ``unit_type`` of ``REPETITIONS``/``TIME``/``DISTANCE``). These are looked
  up live per-sync rather than assumed, since a self-hosted instance's
  fixture data could in principle be re-numbered.
"""

import datetime
import logging

import httpx
from wger_api_client import AuthenticatedClient
from wger_api_client.api.exerciseinfo import exerciseinfo_retrieve
from wger_api_client.api.setting_repetitionunit import setting_repetitionunit_list
from wger_api_client.api.setting_weightunit import setting_weightunit_list
from wger_api_client.api.userprofile import userprofile_retrieve
from wger_api_client.api.weightentry import weightentry_list
from wger_api_client.api.workoutlog import workoutlog_list
from wger_api_client.models.repetition_unit import RepetitionUnit
from wger_api_client.models.weight_entry import WeightEntry
from wger_api_client.models.workout_log import WorkoutLog

logger = logging.getLogger(__name__)

# Wger's default translation language ID for English.
ENGLISH_LANGUAGE_ID = 2


class WgerAPIError(Exception):
    """Raised when the Wger API returns a non-2xx response."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Wger API error {status_code}: {body}")


class WgerClient:
    """HTTP client for a self-hosted Wger instance's REST API.

    All methods are synchronous. No business logic lives here -- callers are
    responsible for interpreting results (unit conversion, alias resolution,
    etc).
    """

    def __init__(self, base_url: str, api_token: str, *, timeout: float = 10) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._client = AuthenticatedClient(
            base_url=self._base_url,
            token=api_token,
            prefix="Token",
            timeout=httpx.Timeout(timeout),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_workout_logs(
        self,
        date_gte: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[WorkoutLog], bool, int]:
        """Fetch a page of the user's workout log entries.

        Args:
            date_gte: Optional ISO date string (``YYYY-MM-DD``); only entries
                on or after this date are returned.
            limit: Page size.
            offset: Row offset for this page.

        Returns:
            (entries, has_more, next_offset) where each entry is a
            ``WorkoutLog`` object (``.exercise``, ``.date``, ``.weight``,
            ``.repetitions``, ``.weight_unit``, ``.repetitions_unit``, ...).

        Raises:
            WgerAPIError: on non-2xx responses.
            httpx.HTTPError: on network failures.
        """
        kwargs: dict = {"limit": limit, "offset": offset, "ordering": "date"}
        if date_gte is not None:
            kwargs["date_gte"] = datetime.datetime.combine(
                datetime.date.fromisoformat(date_gte),
                datetime.time.min,
                tzinfo=datetime.UTC,
            )

        logger.info("Wger API GET /api/v2/workoutlog/")
        response = workoutlog_list.sync_detailed(client=self._client, **kwargs)

        if response.status_code != 200:
            logger.warning(
                "Wger API returned %s for GET /api/v2/workoutlog/",
                response.status_code,
            )
            raise WgerAPIError(
                response.status_code, response.content.decode(errors="replace")
            )

        entries = response.parsed.results
        has_more = bool(response.parsed.next_)
        return entries, has_more, offset + limit

    def get_exercise_name(self, exercise_id: int) -> str | None:
        """Resolve a numeric exercise ID to its human-readable English name.

        Wger's exercise database is normalized (workout logs carry only a
        numeric exercise ID), so this is a second round-trip per unique
        exercise. Returns None if the exercise has no name in any language
        Wger returned, or the lookup itself fails.
        """
        logger.info("Wger API GET /api/v2/exerciseinfo/%s/", exercise_id)
        response = exerciseinfo_retrieve.sync_detailed(
            id=exercise_id, client=self._client
        )

        if response.status_code != 200:
            logger.warning(
                "Wger exercise name lookup failed for exercise %s: status %s",
                exercise_id,
                response.status_code,
            )
            return None

        translations = response.parsed.translations
        if not translations:
            return None

        for translation in translations:
            if translation.language == ENGLISH_LANGUAGE_ID and translation.name:
                return translation.name

        return translations[0].name

    def get_body_weight_unit(self) -> str:
        """Return the unit ("kg"/"lb") this instance records body weight in.

        Body-weight entries carry a bare ``weight`` string with no unit on
        them, unlike workout logs (which reference a numeric
        ``setting-weightunit`` row). The unit lives once on the user's
        profile, so it has to be read separately -- and read rather than
        assumed, since a lifter on an lb profile whose entries are silently
        treated as kg lands roughly 2.2x off.

        Defaults to kg (Wger's own default) when the profile can't be read or
        doesn't declare a unit.
        """
        logger.info("Wger API GET /api/v2/userprofile/")
        response = userprofile_retrieve.sync_detailed(client=self._client)

        if response.status_code != 200:
            logger.warning(
                "Wger API returned %s for GET /api/v2/userprofile/; assuming kg",
                response.status_code,
            )
            return "kg"

        unit = response.parsed.weight_unit
        return unit if unit in ("kg", "lb") else "kg"

    def get_latest_body_weight_entry(self) -> WeightEntry | None:
        """Fetch the lifter's most recent body-weight entry, or None if none.

        ``GET /api/v2/weightentry/`` (``WeightEntryViewSet``), scoped to the
        requesting user server-side like every other endpoint here. Verified
        against Wger's published OpenAPI schema as generated into
        ``wger_api_client``: the list endpoint supports DRF ``ordering`` plus
        the usual ``limit``/``offset`` pagination, and each row is
        ``{id, date, weight, user}`` where ``weight`` is a decimal STRING and
        carries no unit of its own (see :meth:`get_body_weight_unit`).

        ``ordering="-date"`` with ``limit=1`` pushes "which one is newest" to
        the server, so this is a single request regardless of how many years
        of weigh-ins the account holds.

        Raises:
            WgerAPIError: on non-2xx responses.
            httpx.HTTPError: on network failures.
        """
        logger.info("Wger API GET /api/v2/weightentry/")
        response = weightentry_list.sync_detailed(
            client=self._client, limit=1, ordering="-date"
        )

        if response.status_code != 200:
            logger.warning(
                "Wger API returned %s for GET /api/v2/weightentry/",
                response.status_code,
            )
            raise WgerAPIError(
                response.status_code, response.content.decode(errors="replace")
            )

        results = response.parsed.results
        return results[0] if results else None

    def get_weight_units(self) -> dict[int, str]:
        """Return ``{id: name}`` for every weight unit Wger's instance defines.

        A small reference table (a handful of rows) -- one call, no
        pagination loop.
        """
        logger.info("Wger API GET /api/v2/setting-weightunit/")
        response = setting_weightunit_list.sync_detailed(client=self._client)

        if response.status_code != 200:
            logger.warning(
                "Wger API returned %s for GET /api/v2/setting-weightunit/",
                response.status_code,
            )
            raise WgerAPIError(
                response.status_code, response.content.decode(errors="replace")
            )

        return {unit.id: unit.name for unit in response.parsed.results}

    def get_repetition_units(self) -> dict[int, RepetitionUnit]:
        """Return ``{id: RepetitionUnit}`` for every repetition unit defined.

        Returned objects carry ``.name`` and ``.unit_type`` (one of
        ``"REPETITIONS"``, ``"TIME"``, ``"DISTANCE"``).
        """
        logger.info("Wger API GET /api/v2/setting-repetitionunit/")
        response = setting_repetitionunit_list.sync_detailed(client=self._client)

        if response.status_code != 200:
            logger.warning(
                "Wger API returned %s for GET /api/v2/setting-repetitionunit/",
                response.status_code,
            )
            raise WgerAPIError(
                response.status_code, response.content.decode(errors="replace")
            )

        return {unit.id: unit for unit in response.parsed.results}
