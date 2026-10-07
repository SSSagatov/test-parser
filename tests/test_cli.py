"""Exercise CLI validation without contacting production services."""

from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli import cli
from app.config import Settings
from app.models.domain import SearchFilters

runner = CliRunner()


def test_help_exposes_all_requested_filters():
    result = runner.invoke(cli, ["crawl", "--help"], env={"COLUMNS": "200", "NO_COLOR": "1"})
    assert result.exit_code == 0
    for name in (
        "region",
        "city",
        "customer",
        "supplier",
        "date-from",
        "date-to",
        "status",
        "method",
        "budget-min",
        "budget-max",
        "keyword",
        "cpv",
        "announcement-number",
        "lot-number",
        "resume",
        "limit",
        "export",
    ):
        assert f"--{name}" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--limit", "0"],
        ["--budget-min", "10", "--budget-max", "1"],
        ["--date-from", "nonsense"],
        ["--cpv", "12345"],
        ["--export", "unknown"],
    ],
)
def test_invalid_inputs_fail_before_io(args):
    result = runner.invoke(cli, ["crawl", *args])
    assert result.exit_code == 2


def test_zero_budget_is_a_real_filter():
    filters = SearchFilters(budget_min=Decimal(0), budget_max=Decimal(0))
    assert filters.budget_max == 0


def test_environment_example_round_trips():
    settings = Settings(_env_file=Path(".env.example"))
    assert settings.respect_robots
    assert settings.pavlodar_pattern == Settings(_env_file=None).pavlodar_pattern
    assert settings.ows_token is None


def test_doctor_does_not_print_secrets(monkeypatch):
    secret = "sensitive-test-token"
    monkeypatch.setenv("OWS_TOKEN", secret)
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0
    assert secret not in result.stdout
    assert "ows_token_configured" in result.stdout
