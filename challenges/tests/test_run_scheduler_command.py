"""Tests for the run_scheduler management command (TASK-302)."""

from unittest.mock import patch

from apscheduler.triggers.cron import CronTrigger

from challenges.management.commands.run_scheduler import (
    Command,
    build_scheduler,
    close_challenges_job,
)


class TestBuildScheduler:
    def test_registers_close_challenges_job_on_half_hour_cron(self):
        scheduler = build_scheduler()

        job = scheduler.get_job("close_challenges")

        assert job is not None
        assert isinstance(job.trigger, CronTrigger)
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert fields["minute"] == "5,35"
        assert job.max_instances == 1

    def test_skew_setting_shifts_the_cron_minutes(self, settings):
        settings.CLOSE_CHALLENGES_SCHEDULER_SKEW_MINUTES = 2

        scheduler = build_scheduler()

        job = scheduler.get_job("close_challenges")
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert fields["minute"] == "2,32"

    def test_job_target_invokes_close_challenges_job(self):
        scheduler = build_scheduler()

        job = scheduler.get_job("close_challenges")

        assert job.func is close_challenges_job


class TestCloseChallengesJob:
    def test_calls_close_challenges_management_command(self):
        with patch(
            "challenges.management.commands.run_scheduler.call_command"
        ) as mock_call_command:
            close_challenges_job()

        mock_call_command.assert_called_once_with("close_challenges")


class TestRunSchedulerCommandHandle:
    def test_starts_the_built_scheduler(self):
        with patch(
            "challenges.management.commands.run_scheduler.build_scheduler"
        ) as mock_build_scheduler:
            mock_scheduler = mock_build_scheduler.return_value

            Command().handle()

        mock_build_scheduler.assert_called_once_with()
        mock_scheduler.start.assert_called_once_with()
