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
from typing import Any

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

    # Toutes les règles du fichier chargées (leur nombre attendu est fixé plus bas)
    assert f"{len(alert_rules())} rules found" in output


@pytest.mark.parametrize("config", ["alertmanager.yml", "alertmanager-discord.yml"])
def test_alertmanager_configuration_is_valid(config: str) -> None:
    output = run_tool(
        "amtool", "prom/alertmanager", ALERTMANAGER_DIR, "/etc/alertmanager",
        "check-config", f"/etc/alertmanager/{config}",
    )  # fmt: skip

    assert "SUCCESS" in output


# Canal Discord (J7.2) : ce qui demande un humain, rien d'autre
DISCORD_ALERTS = {
    "ShopSimHighErrorRate",
    "ShopSimDown",
    "AgentPilotStateUnavailable",
    "OpsReportWithoutAI",
    "PendingActionTooOld",
}


def receivers(config: str, labels: dict[str, str]) -> list[str]:
    """Receveurs d'une alerte selon l'arbre de routage (`amtool config routes test`)."""
    output = run_tool(
        "amtool", "prom/alertmanager", ALERTMANAGER_DIR, "/etc/alertmanager",
        "config", "routes", "test", f"--config.file=/etc/alertmanager/{config}",
        *(f"{key}={value}" for key, value in labels.items()),
    )  # fmt: skip
    return output.strip().splitlines()[-1].split(",")


@pytest.mark.parametrize("rule", alert_rules(), ids=lambda rule: rule["alert"])
def test_discord_gets_what_needs_a_human_and_agentpilot_gets_everything(
    rule: dict[str, Any],
) -> None:
    labels = {"alertname": rule["alert"], "severity": rule["labels"]["severity"]}

    with_discord = receivers("alertmanager-discord.yml", labels)
    without = receivers("alertmanager.yml", labels)

    expected = ["discord", "agentpilot"] if rule["alert"] in DISCORD_ALERTS else ["agentpilot"]
    assert with_discord == expected
    assert without == ["agentpilot"]


def test_discord_rules_are_the_critical_ones_plus_the_human_ones() -> None:
    critical = {r["alert"] for r in alert_rules() if r["labels"]["severity"] == "critical"}
    assert DISCORD_ALERTS == critical | {"OpsReportWithoutAI", "PendingActionTooOld"}


def test_both_configurations_send_the_same_way_to_agentpilot() -> None:
    """Avec Discord, AgentPilot reçoit tout, regroupé comme sans Discord."""
    base = yaml.safe_load((ALERTMANAGER_DIR / "alertmanager.yml").read_text(encoding="utf-8"))
    found = yaml.safe_load(
        (ALERTMANAGER_DIR / "alertmanager-discord.yml").read_text(encoding="utf-8")
    )
    routes = found["route"].pop("routes")

    assert found["route"] == base["route"]
    assert routes[-1] == {"receiver": "agentpilot"}
    assert all(r["receiver"] == "discord" and r["continue"] is True for r in routes[:-1])
    # Pas de rafale : regroupement par nom d'alerte, 5 min entre deux messages d'un groupe
    assert all(r["group_by"] == ["alertname"] and r["group_interval"] == "5m" for r in routes[:-1])
    receivers_by_name = {r["name"]: r for r in found["receivers"]}
    assert receivers_by_name["agentpilot"] == base["receivers"][0]
    [discord] = receivers_by_name["discord"]["discord_configs"]
    # Adresse dans un fichier écrit au démarrage, jamais dans la configuration
    assert "webhook_url" not in discord and discord["webhook_url_file"].startswith("/tmp/")


def test_annotations_carry_no_customer_data() -> None:
    """Les messages Discord recopient les annotations : seulement des libellés des règles
    (gabarit de route), jamais de valeur venue d'une requête."""
    for rule in alert_rules():
        for text in rule["annotations"].values():
            assert set(re.findall(r"{{(.*?)}}", text)) <= {" $labels.route "}, rule["alert"]


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

    assert len(names) == len(set(names)) == 9
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


# --- Démarrage d'Alertmanager (start.sh), avec une fausse commande `alertmanager` -------------

FAKE_ALERTMANAGER = """#!/bin/sh
printf '%s\\n' "$@" > "$OUT/args"
env > "$OUT/env"
"""
WEBHOOK = "https://discord.com/api/webhooks/123456/secret-token-for-tests"


def start(tmp_path: Path, url: str | None) -> tuple[list[str], str, Path]:
    """Lance start.sh ; renvoie les arguments et l'environnement reçus par Alertmanager, et le
    fichier de l'adresse."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "alertmanager"
    fake.write_text(FAKE_ALERTMANAGER)
    fake.chmod(0o755)
    url_file = tmp_path / "discord_webhook_url"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "OUT": str(tmp_path),
        "ALERTMANAGER_CONFIG_DIR": "/etc/alertmanager",
        "DISCORD_URL_FILE": str(url_file),
    }
    if url is not None:
        env["AGENTPILOT_DISCORD_WEBHOOK_URL"] = url
    done = subprocess.run(
        ["sh", str(ALERTMANAGER_DIR / "start.sh"), "--storage.path=/alertmanager"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert WEBHOOK not in done.stdout + done.stderr
    args = (tmp_path / "args").read_text().splitlines()
    return args, (tmp_path / "env").read_text(), url_file


@pytest.mark.parametrize("url", [None, ""])
def test_without_address_alertmanager_starts_as_before(tmp_path: Path, url: str | None) -> None:
    args, _, url_file = start(tmp_path, url)

    assert args == [
        "--config.file=/etc/alertmanager/alertmanager.yml",
        "--storage.path=/alertmanager",
    ]
    assert not url_file.exists()


def test_with_address_alertmanager_gets_discord_and_a_private_file(tmp_path: Path) -> None:
    args, env, url_file = start(tmp_path, WEBHOOK)

    assert args[0] == "--config.file=/etc/alertmanager/alertmanager-discord.yml"
    assert url_file.read_text() == WEBHOOK
    assert url_file.stat().st_mode & 0o777 == 0o600
    # Retirée de l'environnement d'Alertmanager
    assert WEBHOOK not in env and "AGENTPILOT_DISCORD_WEBHOOK_URL" not in env


def test_an_address_of_another_site_is_refused(tmp_path: Path) -> None:
    args, env, url_file = start(tmp_path, "https://example.com/collect?token=secret-token")

    assert args[0] == "--config.file=/etc/alertmanager/alertmanager.yml"
    assert not url_file.exists() and "secret-token" not in env
