"""Règles d'alerte et configuration de la supervision, fichiers du dépôt.

- `promtool test rules` : chaque règle se déclenche sur des données de panne et reste
  silencieuse sur le trafic normal (`monitoring/prometheus/tests/alerts_test.yml`).
- `promtool check config` et `amtool check-config` : configurations valides.
- Chaque expression de règle est confrontée aux séries publiées, comme les panneaux de Grafana.

`promtool` et `amtool` viennent des images de Docker Compose (mêmes versions), sauf si
`promtool` est installé. Sans l'un ni l'autre, le test échoue, comme ceux qui exigent la base.
"""

import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from agentpilot.support_agent import REPO_ROOT
from agentpilot.tests.test_dashboard import SEVERITIES, alert_rules, exposed, problems

PROMETHEUS_DIR = REPO_ROOT / "monitoring" / "prometheus"
ALERTMANAGER_DIR = REPO_ROOT / "monitoring" / "alertmanager"
RULE_TESTS = PROMETHEUS_DIR / "tests" / "alerts_test.yml"
COMPOSE = REPO_ROOT / "docker-compose.yml"
TIMEOUT_S = 180


def image(name: str) -> str:
    """Image fixée dans docker-compose.yml (`prom/prometheus:v…`)."""
    found = re.search(rf"image:\s*({re.escape(name)}:\S+)", COMPOSE.read_text(encoding="utf-8"))
    assert found is not None, f"{name} absent de docker-compose.yml"
    return found.group(1)


def run_tool(tool: str, image_name: str, mount: Path, target: str, *args: str) -> str:
    """Lance l'outil (installé, sinon dans l'image Docker de Compose) ; échoue s'il échoue."""
    if tool == "promtool" and shutil.which("promtool"):
        command = ["promtool", *args]
        cwd: Path | None = mount
    elif shutil.which("docker"):
        command = [
            "docker", "run", "--rm", "-v", f"{mount}:{target}:ro", "-w", target,
            "--entrypoint", tool, image(image_name), *args,
        ]  # fmt: skip
        cwd = None
    else:
        pytest.fail(
            f"{tool} introuvable : installer Docker (ou promtool) pour tester la supervision"
        )
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S)
    output = done.stdout + done.stderr
    assert done.returncode == 0, f"{' '.join(command)}\n{output}"
    return output


def test_rules_pass_their_promtool_tests() -> None:
    output = run_tool(
        "promtool", "prom/prometheus", PROMETHEUS_DIR, "/etc/prometheus",
        "test", "rules", "tests/alerts_test.yml",
    )  # fmt: skip

    assert "SUCCESS" in output


def test_prometheus_configuration_and_rules_are_valid() -> None:
    output = run_tool(
        "promtool", "prom/prometheus", PROMETHEUS_DIR, "/etc/prometheus",
        "check", "config", "/etc/prometheus/prometheus.yml",
    )  # fmt: skip

    assert "8 rules found" in output


def test_alertmanager_configuration_is_valid() -> None:
    output = run_tool(
        "amtool", "prom/alertmanager", ALERTMANAGER_DIR, "/etc/alertmanager",
        "check-config", "/etc/alertmanager/alertmanager.yml",
    )  # fmt: skip

    assert "SUCCESS" in output


def test_rule_expressions_match_the_exposed_series() -> None:
    known = exposed()

    found = {
        f"{rule['alert']} : {problem}"
        for rule in alert_rules()
        for problem in problems(rule["expr"], known)
    }

    assert found == set()


def test_rules_are_symptoms_with_severity_and_french_annotations() -> None:
    rules = alert_rules()
    names = [rule["alert"] for rule in rules]

    assert len(names) == len(set(names)) == 8
    for rule in rules:
        assert rule["labels"]["severity"] in SEVERITIES
        assert {"summary", "description"} <= rule["annotations"].keys()
        # Jamais l'état des pannes : seulement des symptômes
        assert "chaos" not in rule["expr"].lower()


def test_every_rule_has_a_firing_case_and_a_silent_case() -> None:
    tests = yaml.safe_load(RULE_TESTS.read_text(encoding="utf-8"))["tests"]
    cases: dict[str, set[bool]] = defaultdict(set)
    for group in tests:
        for case in group["alert_rule_test"]:
            cases[case["alertname"]].add(bool(case["exp_alerts"]))

    for rule in alert_rules():
        assert cases[rule["alert"]] == {True, False}, rule["alert"]
