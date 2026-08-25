import logging
from unittest import mock

import pytest
from django.core.management import call_command

from core.management.commands.seed_all import SEED_COMMANDS
from core.models import LiftAlias, LiftAliasSource
from fitnessvolt.models import FitnessVoltLiftAlias
from liftosaur.models import Lift


@pytest.mark.django_db
def test_seed_all_runs_every_seed_command():
    with mock.patch(
        "core.management.commands.seed_all.call_command"
    ) as mock_call_command:
        call_command("seed_all")

    called = [c.args[0] for c in mock_call_command.call_args_list]
    assert called == list(SEED_COMMANDS)


@pytest.mark.django_db
def test_seed_all_populates_reference_tables_idempotently():
    call_command("seed_all")
    counts = (
        Lift.objects.count(),
        LiftAlias.objects.filter(source=LiftAliasSource.LIFTOSAUR).count(),
        FitnessVoltLiftAlias.objects.count(),
        LiftAlias.objects.filter(source=LiftAliasSource.HEVY).count(),
        LiftAlias.objects.filter(source=LiftAliasSource.STRONG).count(),
    )
    assert all(count > 0 for count in counts)

    call_command("seed_all")
    assert counts == (
        Lift.objects.count(),
        LiftAlias.objects.filter(source=LiftAliasSource.LIFTOSAUR).count(),
        FitnessVoltLiftAlias.objects.count(),
        LiftAlias.objects.filter(source=LiftAliasSource.HEVY).count(),
        LiftAlias.objects.filter(source=LiftAliasSource.STRONG).count(),
    )


@pytest.mark.django_db
def test_seed_all_propagates_and_logs_failure(caplog):
    boom = RuntimeError("seed exploded")

    def fake_call_command(name, *args, **kwargs):
        if name == "seed_liftosaur_lifts":
            raise boom

    with (
        mock.patch(
            "core.management.commands.seed_all.call_command",
            side_effect=fake_call_command,
        ),
        caplog.at_level(logging.ERROR, logger="core"),
        pytest.raises(RuntimeError, match="seed exploded"),
    ):
        call_command("seed_all")

    assert "seed_liftosaur_lifts failed" in caplog.text
