"""Guardrail tests for the repository scaffold (no cloud credentials required)."""

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("package", ["ingest", "rag", "api", "eval"])
def test_packages_importable(package: str) -> None:
    importlib.import_module(package)


@pytest.mark.parametrize(
    "pattern",
    [".env", "*.tfstate", "*.tfvars", "!*.tfvars.example", "credentials/", ".terraform/"],
)
def test_gitignore_protects_secrets_and_state(pattern: str) -> None:
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert pattern in lines


def test_example_configs_exist() -> None:
    assert (ROOT / ".env.example").is_file()
    assert (ROOT / "infra/terraform/envs/dev/terraform.tfvars.example").is_file()
