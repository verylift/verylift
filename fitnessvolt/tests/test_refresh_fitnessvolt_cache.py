"""Tests for the refresh_fitnessvolt_cache management command.

The command is thin, but it is the only operator-facing surface for the
FitnessVolt cache, and its whole job is turning refresh_cache's summary
mapping into something readable at a terminal. An empty summary means every
population's pull failed -- reporting that as success would leave an operator
believing a stale cache had just been refreshed.

refresh_cache is patched: it walks the live FitnessVolt API, which the
project's testing standards keep out of tests.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command


def run():
    out = StringIO()
    call_command("refresh_fitnessvolt_cache", stdout=out)
    return out.getvalue()


def test_reports_each_populations_outcome():
    summary = {"strength_level": "inserted:2026-09-01", "fitnessvolt": "noop"}
    with patch(
        "fitnessvolt.management.commands.refresh_fitnessvolt_cache.refresh_cache",
        return_value=summary,
    ):
        output = run()

    for population, outcome in summary.items():
        assert f"{population}: {outcome}" in output


def test_an_empty_summary_is_reported_as_a_failure_not_a_silent_success():
    """refresh_cache swallows every per-population error and returns {} when
    nothing refreshed, so the command's own output is the operator's only
    signal that the run achieved nothing.
    """
    with patch(
        "fitnessvolt.management.commands.refresh_fitnessvolt_cache.refresh_cache",
        return_value={},
    ):
        output = run()

    assert "No populations refreshed" in output
