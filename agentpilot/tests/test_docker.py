"""AgentPilot dans Docker Compose (J7.1), par la lecture des fichiers : réseau Docker, ports sur
127.0.0.1, aucun secret de trop dans le conteneur, image sans vérité terrain ni CLAUDE.md
développeur, utilisateur non root. Aucun conteneur lancé."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITORING = REPO_ROOT / "monitoring"


def load(path: Path) -> Any:  # noqa: ANN401
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    return load(REPO_ROOT / "docker-compose.yml")["services"]


@pytest.fixture(scope="module")
def dockerfile() -> list[str]:
    """Instructions du Dockerfile d'AgentPilot, sans les commentaires."""
    text = (REPO_ROOT / "agentpilot" / "Dockerfile").read_text(encoding="utf-8")
    joined = re.sub(r"\\\n", " ", text)
    return [line.strip() for line in joined.splitlines() if line.strip() and line[0] != "#"]


# --- Compose -----------------------------------------------------------------------------------


def test_no_service_uses_the_host_network(services: dict[str, Any]) -> None:
    assert [name for name, spec in services.items() if "network_mode" in spec] == []


def test_every_published_port_is_on_localhost_only(services: dict[str, Any]) -> None:
    ports = [str(port) for spec in services.values() for port in spec.get("ports", [])]
    assert ports and all(port.startswith("127.0.0.1:") for port in ports)


def test_agentpilot_service_is_published_and_waits_for_its_dependencies(
    services: dict[str, Any],
) -> None:
    agentpilot = services["agentpilot"]
    assert agentpilot["build"]["dockerfile"] == "agentpilot/Dockerfile"
    assert agentpilot["ports"] == ["127.0.0.1:8001:8001"]
    assert {name: dep["condition"] for name, dep in agentpilot["depends_on"].items()} == {
        "db": "service_healthy",
        "app": "service_healthy",
    }
    # Santé lue sur une page qui lit la base
    assert "/control/status" in " ".join(agentpilot["healthcheck"]["test"])
    assert "./logs:/srv/logs" in agentpilot["volumes"]


def test_agentpilot_never_gets_the_chaos_token(services: dict[str, Any]) -> None:
    """Ni `env_file` (qui chargerait tout `.env`), ni CHAOS_TOKEN sous aucune forme."""
    agentpilot = services["agentpilot"]
    assert "env_file" not in agentpilot
    assert "CHAOS_TOKEN" not in yaml.safe_dump(agentpilot)
    # Seul ShopSim le reçoit
    assert [name for name, spec in services.items() if "CHAOS_TOKEN" in yaml.safe_dump(spec)] == [
        "app"
    ]


def test_agentpilot_environment(services: dict[str, Any]) -> None:
    env = services["agentpilot"]["environment"]
    # Clé d'API par l'environnement du conteneur (interpolation de Compose), jamais en dur
    assert env["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY:-}"
    assert "@db:5432/" in env["AGENTPILOT_DATABASE_URL"]
    assert env["AGENTPILOT_LOG_DIR"] == "/srv/logs"
    # Démarrer ne dépense rien, par défaut
    assert env["AGENTPILOT_PAUSE_ON_START"] == "${AGENTPILOT_PAUSE_ON_START:-1}"
    for name in (
        "AGENTPILOT_DAILY_BUDGET_USD",
        "AGENTPILOT_OPS_ARCHITECTURE",
        "AGENTPILOT_OPS_MAX_PER_HOUR",
    ):
        assert env[name].startswith(f"${{{name}:-")
    # Sources des agents par les noms des services Docker
    assert env["SHOPSIM_URL"] == "http://app:8000"
    assert env["PROMETHEUS_URL"] == "http://prometheus:9090"
    assert env["ALERTMANAGER_URL"] == "http://alertmanager:9093"
    assert env["AGENTPILOT_URL"] == "http://localhost:8001"


def test_agentpilot_runs_as_the_host_user_not_root(services: dict[str, Any]) -> None:
    user = services["agentpilot"]["user"]
    assert user == "${UID:-1000}:${GID:-1000}"
    assert not user.startswith(("0", "root"))


def test_monitoring_reaches_docker_service_names() -> None:
    prometheus = load(MONITORING / "prometheus" / "prometheus.yml")
    targets = {
        job["job_name"]: job["static_configs"][0]["targets"] for job in prometheus["scrape_configs"]
    }
    assert targets == {"shopsim": ["app:8000"], "agentpilot": ["agentpilot:8001"]}
    [alertmanagers] = prometheus["alerting"]["alertmanagers"]
    assert alertmanagers["static_configs"][0]["targets"] == ["alertmanager:9093"]

    alertmanager = load(MONITORING / "alertmanager" / "alertmanager.yml")
    [receiver] = alertmanager["receivers"]
    assert receiver["webhook_configs"][0]["url"] == "http://agentpilot:8001/alerts/webhook"

    grafana = load(MONITORING / "grafana" / "provisioning" / "datasources" / "prometheus.yml")
    assert [source["url"] for source in grafana["datasources"]] == ["http://prometheus:9090"]

    everything = "".join(
        path.read_text(encoding="utf-8")
        for path in (
            MONITORING / "prometheus" / "prometheus.yml",
            MONITORING / "alertmanager" / "alertmanager.yml",
            MONITORING / "grafana" / "provisioning" / "datasources" / "prometheus.yml",
        )
    )
    assert "localhost" not in everything and "127.0.0.1" not in everything


# --- Image -------------------------------------------------------------------------------------


def test_image_runs_as_a_non_root_user(dockerfile: list[str]) -> None:
    users = [line.split()[1] for line in dockerfile if line.startswith("USER ")]
    assert users and users[-1] not in ("root", "0")
    assert any("useradd" in line and "--uid 1000" in line for line in dockerfile)
    assert dockerfile[-1].startswith("CMD") and "0.0.0.0" in dockerfile[-1]


def test_image_copies_no_truth_no_developer_instructions_no_secret(
    dockerfile: list[str],
) -> None:
    copies = [line.split()[1:-1] for line in dockerfile if line.startswith("COPY ")]
    sources = [source for sources in copies for source in sources if not source.startswith("--")]
    assert "." not in sources and "./" not in sources  # jamais tout le dépôt
    assert not [s for s in sources if s.endswith(".jsonl") or s in ("evals", "evals/")]
    assert not [s for s in sources if Path(s).name == "CLAUDE.md" or s.startswith(".env")]
    # De evals/, des modules sans données : seuils et énumérations
    assert sorted(s for s in sources if s.startswith("evals/")) == [
        "evals/__init__.py",
        "evals/ground_truth.py",
        "evals/policy.py",
    ]
    # Ni tests, ni développement
    assert any("rm -rf" in line and "agentpilot/tests" in line for line in dockerfile)


def test_env_files_are_excluded_from_the_build_context() -> None:
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".env", ".env.*", "logs", "evals/results", "runs"} <= set(ignored)


def test_ground_truth_module_in_the_image_holds_no_data() -> None:
    """`evals/ground_truth.py` n'est que le schéma (énumérations) : la vérité terrain est dans
    `evals/tickets.jsonl`, qui ne va jamais dans l'image."""
    text = (REPO_ROOT / "evals" / "ground_truth.py").read_text(encoding="utf-8")
    assert '.jsonl"' not in text and "open(" not in text and "read_text" not in text


# --- Canal Discord (J7.2) ----------------------------------------------------------------------


def test_discord_address_goes_only_to_alertmanager_and_agentpilot(
    services: dict[str, Any],
) -> None:
    holders = [name for name, spec in services.items() if "DISCORD" in yaml.safe_dump(spec).upper()]
    assert sorted(holders) == ["agentpilot", "alertmanager"]
    for name in holders:
        # Vide par défaut : sans adresse, le système marche comme avant
        env = services[name]["environment"]
        assert env["AGENTPILOT_DISCORD_WEBHOOK_URL"] == "${AGENTPILOT_DISCORD_WEBHOOK_URL:-}"


def test_alertmanager_starts_through_its_script(services: dict[str, Any]) -> None:
    alertmanager = services["alertmanager"]
    assert alertmanager["entrypoint"] == ["/bin/sh", "/etc/alertmanager/start.sh"]
    # Le script choisit la configuration : jamais en dur dans la commande
    assert not any("config.file" in arg for arg in alertmanager["command"])
    assert "./monitoring/alertmanager:/etc/alertmanager:ro" in alertmanager["volumes"]
    assert (REPO_ROOT / "monitoring" / "alertmanager" / "start.sh").is_file()
