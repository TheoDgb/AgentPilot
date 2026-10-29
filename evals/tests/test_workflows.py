"""Garde-fous des workflows GitHub Actions (J5.4) : sécurité et cohérence avec le dépôt.

Ces tests ne lancent rien : ils lisent `.github/workflows/*.yml` et vérifient ce qui, s'il
changeait par mégarde, exposerait un secret ou ferait mentir la CI.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentpilot.support_agent import DEFAULT_PROMPT, PROMPTS, REPO_ROOT
from evals.ground_truth import TicketGroundTruth
from evals.run import GROUND_TRUTH

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TESTS = WORKFLOWS / "tests.yml"
EVALUATION = WORKFLOWS / "evaluation.yml"
# Action épinglée par un SHA de commit complet, version en commentaire
PINNED = re.compile(r"^\s*(-\s+)?uses:\s+[\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+(\.\d+)*\s*$")


def load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 lit la clé `on` comme le booléen True
    return workflow.get("on", workflow.get(True)) or {}


def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


@pytest.mark.parametrize("path", [TESTS, EVALUATION], ids=["tests", "evaluation"])
def test_every_action_is_pinned_by_full_commit_sha(path: Path) -> None:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "uses:" in line]
    assert lines
    for line in lines:
        assert PINNED.match(line), f"action non épinglée par SHA complet : {line.strip()}"


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v4",
        "      - uses: actions/checkout@main",
        "      - uses: actions/checkout@3d3c42e # v7.0.1",
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    ],
)
def test_pin_check_rejects_tags_branches_and_short_shas(line: str) -> None:
    assert PINNED.match(line) is None


@pytest.mark.parametrize("path", [TESTS, EVALUATION], ids=["tests", "evaluation"])
def test_read_only_token_and_never_pull_request_target(path: Path) -> None:
    workflow = load(path)
    assert workflow["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in triggers(workflow)
    # Le dépôt n'est pas laissé authentifié dans l'espace de travail
    checkouts = [s for s in steps(workflow) if "actions/checkout@" in s.get("uses", "")]
    assert checkouts and all(s["with"]["persist-credentials"] is False for s in checkouts)


def test_tests_workflow_needs_no_secret_and_uses_the_test_database() -> None:
    workflow = load(TESTS)
    assert set(triggers(workflow)) == {"push", "pull_request"}
    assert "secrets." not in TESTS.read_text(encoding="utf-8")
    postgres = workflow["jobs"]["tests"]["services"]["postgres"]
    # `TEST_DATABASE_URL` par défaut des fixtures : localhost:5433/shopsim_test
    assert postgres["ports"] == ["5433:5432"]
    assert postgres["env"]["POSTGRES_DB"] == "shopsim_test"
    runs = " ".join(s.get("run", "") for s in steps(workflow))
    assert "ruff check" in runs and "ruff format --check" in runs and "pytest" in runs


def test_evaluation_is_manual_single_and_in_the_protected_environment() -> None:
    workflow = load(EVALUATION)
    assert set(triggers(workflow)) == {"workflow_dispatch"}
    assert workflow["concurrency"] == {"group": "evaluation", "cancel-in-progress": False}
    assert workflow["jobs"]["evaluation"]["environment"] == "evaluation"


def test_api_key_only_in_the_evaluation_step() -> None:
    workflow = load(EVALUATION)
    with_secret = [s for s in steps(workflow) if "secrets." in yaml.safe_dump(s)]
    assert [s["name"] for s in with_secret] == ["Évaluation"]
    assert with_secret[0]["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    assert "evals.run" in with_secret[0]["run"]
    # Ni au niveau du workflow ou du job, ni ailleurs dans le fichier
    assert "env" not in workflow and "env" not in workflow["jobs"]["evaluation"]
    assert EVALUATION.read_text(encoding="utf-8").count("secrets.") == 1


def test_chaos_token_is_random_masked_and_never_in_the_environment() -> None:
    workflow = load(EVALUATION)
    [step] = [s for s in steps(workflow) if s.get("name") == "Jeton Chaos"]
    script = step["run"]
    assert "openssl rand" in script
    assert script.index("::add-mask::") < script.index("> .env")
    text = EVALUATION.read_text(encoding="utf-8")
    assert "GITHUB_ENV" not in text
    for s in steps(workflow):
        assert "CHAOS_TOKEN" not in s.get("env", {})
    # `.env` effacé à la fin, même après un échec
    [stop] = [s for s in steps(workflow) if s.get("name") == "Arrêt"]
    assert stop["if"] == "always()" and "rm -f .env" in stop["run"]


def test_inputs_are_never_interpolated_in_a_script() -> None:
    """Injection de commande : un paramètre passe par `env:`, jamais par `${{ inputs.… }}`."""
    for s in steps(load(EVALUATION)):
        assert "${{" not in s.get("run", ""), s.get("name")


def test_inputs_match_the_repository() -> None:
    inputs = triggers(load(EVALUATION))["workflow_dispatch"]["inputs"]
    truth = {
        TicketGroundTruth.model_validate_json(line).ticket_id
        for line in GROUND_TRUTH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    defaults = {int(t) for t in inputs["tickets"]["default"].split(",")}
    assert defaults <= truth
    assert set(inputs["prompt"]["options"]) == set(PROMPTS)
    assert inputs["prompt"]["default"] == DEFAULT_PROMPT


def test_grading_checks_the_versioned_thresholds_and_artifacts_skip_chaos_logs() -> None:
    workflow = load(EVALUATION)
    [grade] = [s for s in steps(workflow) if s.get("name") == "Notation et seuils"]
    assert "--thresholds evals/thresholds.toml" in " ".join(grade["run"].split())
    [upload] = [s for s in steps(workflow) if "upload-artifact@" in s.get("uses", "")]
    assert "chaos" not in upload["with"]["path"]
    assert upload["if"] == "always()"


def test_readme_has_the_tests_badge() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "/actions/workflows/tests.yml/badge.svg" in readme
