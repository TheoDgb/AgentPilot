"""Évaluation de l'agent Ops (J6.3) : déroule les scénarios de `evals/incidents.jsonl`.

    python -m evals.ops_run --scenarios db_errors,false_alert --name verif
    python -m evals.ops_run --passes 2 --max-budget-usd 5

Prérequis : la pile Docker en marche (ShopSim, Prometheus, Alertmanager), le service AgentPilot
**arrêté** (le lanceur le démarre lui-même), `CHAOS_TOKEN` dans `.env` seulement, la clé d'API.

Déroulé : pannes désactivées, remise à zéro complète (`agentpilot.reset.reset_all`), puis
AgentPilot et le générateur de trafic lancés en sous-processus (liés au lanceur). Pour chaque
scénario : attente d'un état propre (aucune panne, aucune alerte active, `history_min` minutes
de trafic normal, latences et erreurs normales selon Prometheus), pannes activées et arrêtées
aux minutes prévues, charge de remboursements ou fausse alerte, attente de l'alerte attendue,
enquête de l'agent Ops (`POST /runs/ops/{alert_id}`), puis arrêt : **la charge d'abord** (fin
des envois en cours), ensuite les pannes, puis la fausse alerte résolue.

La boucle de l'agent Support reste en pause pendant toute l'évaluation : le lanceur ne la
relance jamais et s'arrête (`aborted`) s'il la trouve en marche.

Fin, toujours (fin normale, Ctrl+C, SIGTERM, erreur) : charge arrêtée et vidée, pannes
désactivées, fausse alerte résolue, générateur et AgentPilot arrêtés. Lanceur tué (`kill -9`) :
son gardien (`evals.ops_guard`) fait le même nettoyage, dans le même ordre.

Le lanceur n'écrit jamais dans les logs que lit l'agent Ops : son journal va dans
`logs/ops_eval.jsonl`, hors de la liste fixe des services lisibles.

Budget : une enquête n'est lancée que si la dépense de l'évaluation plus 1 $ (plafond d'une
enquête) reste sous `--max-budget-usd`. Erreur d'infrastructure pendant une enquête : la série
s'arrête, le scénario est `interrupted`, jamais une faute de l'agent.

Résultats : `evals/results/ops-<date>-<nom>[-pN].json`, réécrits après chaque scénario ; notés
par `python -m evals.ops_grade`.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import IO, Any, Protocol

import httpx

from agentpilot import ops_agent
from agentpilot.sdk_runner import INFRA_ERRORS
from agentpilot.support_agent import AgentRunError, load_api_key
from evals.incident_truth import (
    INCIDENTS_PATH,
    AlertTarget,
    IncidentScenario,
    InjectedAlert,
    Investigation,
    load_scenarios,
    truth_sha256,
)
from evals.ops_results import (
    FaultWindow,
    OpsEvalReport,
    OpsEvalSummary,
    RunStatus,
    ScenarioResult,
    ScenarioStatus,
    unit_label,
)
from evals.run import REPO_ROOT, RESULTS_DIR, code_version, config_name
from mcp_server.ops.metrics_tools import promql
from mcp_server.parent_watch import die_with_parent
from observability.logs import configure_isolated, log_event
from shopsim import chaos_cli

logger = logging.getLogger("evals.ops_run")

SERVICE_URL = "http://localhost:8001"
SHOPSIM_URL = "http://localhost:8000"
PROMETHEUS_URL = "http://localhost:9090"
ALERTMANAGER_URL = "http://localhost:9093"
ENV_FILE = REPO_ROOT / ".env"
LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = "ops_eval.jsonl"

POLL_S = 10.0
# Délai maximal d'une enquête suivie (l'agent s'arrête seul à 10 min)
RUN_TIMEOUT = timedelta(minutes=12)
# Au-delà de `history_min`, délai pour retrouver un état propre
CLEAN_EXTRA = timedelta(minutes=20)
# Une alerte « née pendant ce scénario » : début au plus tôt une minute avant son départ
ALERT_MARGIN = timedelta(minutes=1)
DEFAULT_BUDGET_USD = Decimal("5")
RESERVE_USD = Decimal(str(ops_agent.MAX_BUDGET_USD))
# Seuils d'un état normal mesuré par Prometheus (p95 et part de 5xx sur 5 min)
NORMAL_P95_S = 1.0
NORMAL_ERROR_RATE = 0.01
# Commandes du seed dont la charge rembourse les paiements (refusés pendant la panne)
LOAD_ORDERS = tuple(range(1, 21))
SERVICE_STOP_S = 30.0


class Aborted(Exception):
    """Arrêt de l'évaluation : état propre introuvable, boucle relancée, enquête sans fin…"""


class InfraStop(Exception):
    """Erreur d'infrastructure pendant une enquête : jamais une faute de l'agent."""


class BudgetExhausted(Exception):
    pass


class Refused(Exception):
    pass


# --- Ce que le lanceur pilote ------------------------------------------------------------


class Stack(Protocol):
    """Pannes, alertes, service AgentPilot et Prometheus (remplacés par des faux en test)."""

    async def turn_on(self, fault: str) -> None: ...
    async def turn_off(self, fault: str) -> None: ...
    async def turn_off_all(self) -> None: ...
    async def active_faults(self) -> list[str]: ...
    async def firing_alerts(self) -> list[dict[str, Any]]: ...
    async def post_alert(self, payload: dict[str, Any]) -> None: ...
    async def loop_paused(self) -> bool: ...
    async def start_run(self, alert_id: str) -> dict[str, Any]: ...
    async def get_run(self, run_id: str) -> dict[str, Any]: ...
    async def abnormal_metrics(self) -> str | None: ...


class LoadHandle(Protocol):
    pid: int | None

    def stop(self) -> int:
        """Plus aucun envoi ; attend la fin de l'envoi en cours ; renvoie le nombre d'envois."""
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# --- Alertes -------------------------------------------------------------------------------


def alert_payload(
    alert: InjectedAlert, at: datetime, fingerprint: str, resolved: bool
) -> dict[str, Any]:
    """Corps du webhook d'Alertmanager, tel que l'envoie un vrai Alertmanager."""
    status = "resolved" if resolved else "firing"
    labels = {"alertname": alert.alertname, "severity": alert.severity}
    if alert.route is not None:
        labels["route"] = alert.route
    entry = {
        "status": status,
        "labels": labels,
        "annotations": {"summary": alert.summary},
        "startsAt": at.isoformat(),
        "endsAt": datetime.now(UTC).isoformat() if resolved else "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint,
    }
    return {"version": "4", "status": status, "alerts": [entry]}


def _when(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def pick_alert(
    alerts: Sequence[Mapping[str, Any]],
    target: AlertTarget,
    scenario: IncidentScenario,
    started: datetime,
) -> Mapping[str, Any] | None:
    """L'occurrence à confier à l'agent : bon nom, bonne route, née pendant ce scénario (ou,
    pour un enchaînement, avant l'étape `started_before_step`). La plus ancienne d'abord."""
    candidates = []
    for alert in alerts:
        if alert.get("alertname") != target.alertname or alert.get("route") != target.route:
            continue
        begun = _when(alert["starts_at"])
        if begun < started - ALERT_MARGIN:
            continue
        if target.started_before_step is not None:
            step = scenario.steps[target.started_before_step]
            if begun >= started + timedelta(minutes=step.start_min):
                continue
        candidates.append((begun, alert))
    return min(candidates, key=lambda c: c[0])[1] if candidates else None


# --- Une évaluation (un passage) -----------------------------------------------------------


@dataclass
class Running:
    """Une panne activée pendant le scénario en cours."""

    index: int
    window: FaultWindow


@dataclass
class Pending:
    """Une enquête du scénario en cours, pas encore terminée."""

    investigation: Investigation
    result: ScenarioResult
    alert: Mapping[str, Any] | None = None
    run_id: str | None = None
    run_started: datetime | None = None


class OpsEvaluation:
    def __init__(
        self,
        stack: Stack,
        start_load: Callable[[int], LoadHandle],
        clock: Clock,
        report: OpsEvalReport,
        path: Path,
        *,
        max_budget: Decimal,
        last_fault_end: datetime,
        notify: Callable[[dict[str, Any]], None] = lambda update: None,
        spent_before: Decimal = Decimal(0),
    ) -> None:
        self.stack = stack
        self.start_load = start_load
        self.clock = clock
        self.report = report
        self.path = path
        self.max_budget = max_budget
        self.last_fault_end = last_fault_end
        self.notify = notify
        self.spent_before = spent_before
        self.spent = Decimal(0)
        self.load: LoadHandle | None = None
        # Enquêtes du scénario dont la charge est en cours (envois comptés sur chacune)
        self.load_results: list[ScenarioResult] = []
        # Enquêtes du scénario en cours
        self.current: list[ScenarioResult] = []
        self.fake_alert: dict[str, Any] | None = None
        self.running: list[Running] = []
        # Étapes déjà activées dans le scénario en cours
        self.started_steps: set[int] = set()

    # --- Arrêt : toujours la charge d'abord ---

    async def wind_down(self) -> None:
        """Charge arrêtée et vidée, **puis** pannes désactivées, puis fausse alerte résolue.
        Sans effet si rien n'est en cours ; appelé à chaque fin de scénario et à l'arrêt."""
        if self.load is not None:
            sent = await asyncio.to_thread(self.load.stop)
            for result in self.load_results:
                result.refunds_sent += sent
            self.load, self.load_results = None, []
            self.notify({"load": None})
        if self.running:
            await self.stack.turn_off_all()
            now = self.clock.now()
            for active in self.running:
                active.window.stopped_at = active.window.stopped_at or now
            self.running = []
            self.last_fault_end = now
        if self.fake_alert is not None:
            await self.stack.post_alert(self.fake_alert)
            self.fake_alert = None
            self.notify({"fake_alert": None})

    # --- État propre ---

    async def _check_loop(self) -> None:
        if not await self.stack.loop_paused():
            raise Aborted("boucle de l'agent Support relancée pendant l'évaluation")

    async def wait_clean(self, scenario: IncidentScenario) -> None:
        history = timedelta(minutes=scenario.history_min)
        deadline = self.clock.now() + history + CLEAN_EXTRA
        while True:
            await self._check_loop()
            reasons = []
            if await self.stack.active_faults():
                reasons.append("panne active")
                await self.stack.turn_off_all()
                self.last_fault_end = self.clock.now()
            if await self.stack.firing_alerts():
                reasons.append("alerte active")
            if self.clock.now() - self.last_fault_end < history:
                reasons.append("historique de trafic normal trop court")
            abnormal = await self.stack.abnormal_metrics()
            if abnormal:
                reasons.append(abnormal)
            if not reasons:
                return
            if self.clock.now() >= deadline:
                raise Aborted(f"état propre introuvable : {', '.join(reasons)}")
            await self.clock.sleep(POLL_S)

    # --- Un scénario ---

    def _minutes(self, started: datetime) -> float:
        return (self.clock.now() - started).total_seconds() / 60

    async def _steps(
        self, scenario: IncidentScenario, started: datetime, results: list[ScenarioResult]
    ) -> None:
        """Active et arrête les pannes aux minutes prévues ; la charge suit son étape. Chaque
        fenêtre de panne est la même pour toutes les enquêtes du scénario."""
        elapsed = self._minutes(started)
        for index, step in enumerate(scenario.steps):
            running = next((r for r in self.running if r.index == index), None)
            if index not in self.started_steps and elapsed >= step.start_min:
                await self.stack.turn_on(step.fault.value)
                window = FaultWindow(fault=step.fault.value, started_at=self.clock.now())
                for result in results:
                    result.faults.append(window)
                self.running.append(Running(index, window))
                self.started_steps.add(index)
                log_event(logger, "ops_eval.fault_on", phase=step.fault.value)
                load = scenario.load
                if load is not None and load.during_step == index:
                    # Lancement direct (un `Popen`, immédiat) : une interruption ne peut pas
                    # tomber entre le lancement et l'enregistrement, ce qui oublierait la charge
                    self.load = self.start_load(load.per_minute)
                    self.load_results = results
                    self.notify({"load": self.load.pid})
            elif running is not None and step.stop_min is not None and elapsed >= step.stop_min:
                load = scenario.load
                if load is not None and load.during_step == index and self.load is not None:
                    # La charge d'abord : aucun remboursement après la panne
                    sent = await asyncio.to_thread(self.load.stop)
                    for result in results:
                        result.refunds_sent += sent
                    self.load, self.load_results = None, []
                    self.notify({"load": None})
                await self.stack.turn_off(step.fault.value)
                running.window.stopped_at = self.clock.now()
                self.running.remove(running)
                self.last_fault_end = self.clock.now()

    def _budget_allows(self) -> bool:
        return self.spent_before + self.spent + RESERVE_USD <= self.max_budget

    async def scenario(self, scenario: IncidentScenario) -> list[ScenarioResult]:
        results = [
            ScenarioResult(
                scenario_id=scenario.scenario_id,
                investigation=investigation.name,
                status=ScenarioStatus.INTERRUPTED,
            )
            for investigation in scenario.investigations
        ]
        self.current = results
        self.report.scenarios.extend(results)
        try:
            await self.wait_clean(scenario)
            await self._play(scenario, results)
        finally:
            await self.wind_down()
            for result in results:
                result.finished_at = self.clock.now()
            self.save()
        return results

    async def _play(self, scenario: IncidentScenario, results: list[ScenarioResult]) -> None:
        """Les enquêtes du scénario, l'une après l'autre : chacune attend son alerte, puis son
        heure. Une alerte trouvée reste celle de l'enquête, même résolue entre-temps."""
        started = self.clock.now()
        self.started_steps = set()
        for result in results:
            result.started_at = started
        if scenario.inject_alert is not None:
            fingerprint = secrets.token_hex(8)
            await self.stack.post_alert(
                alert_payload(scenario.inject_alert, started, fingerprint, resolved=False)
            )
            self.fake_alert = alert_payload(
                scenario.inject_alert, started, fingerprint, resolved=True
            )
            self.notify({"fake_alert": self.fake_alert})
        pending = [
            Pending(investigation, result)
            for investigation, result in zip(scenario.investigations, results, strict=True)
        ]
        while True:
            await self._steps(scenario, started, results)
            await self._check_loop()
            elapsed = self._minutes(started)
            waiting = [i for i in pending if i.alert is None]
            firing = await self.stack.firing_alerts() if waiting else []
            for item in waiting:
                item.alert = pick_alert(firing, item.investigation.alert, scenario, started)
                if item.alert is not None:
                    item.result.alert_id = uuid.UUID(str(item.alert["id"]))
                    item.result.alert_started_at = _when(item.alert["starts_at"])
            for item in pending:
                if item.alert is None and elapsed >= scenario.alert_timeout_min:
                    item.result.status = ScenarioStatus.NO_ALERT
                    item.result.reason = (
                        f"alerte attendue absente après {scenario.alert_timeout_min:g} min"
                    )
            pending = [i for i in pending if i.result.status is not ScenarioStatus.NO_ALERT]
            running = next((i for i in pending if i.run_id is not None), None)
            if running is None:
                ready = next(
                    (
                        i
                        for i in pending
                        if i.alert is not None and elapsed >= i.investigation.investigate_after_min
                    ),
                    None,
                )
                if ready is not None:
                    if not self._budget_allows():
                        for item in pending:
                            item.result.status = ScenarioStatus.NOT_RUN
                            item.result.reason = "budget"
                        raise BudgetExhausted
                    assert ready.alert is not None
                    created = await self.stack.start_run(str(ready.alert["id"]))
                    ready.run_id, ready.run_started = str(created["id"]), self.clock.now()
                    ready.result.run_id = uuid.UUID(ready.run_id)
                    ready.result.trace_id = created.get("trace_id")
                    log_event(
                        logger,
                        "ops_eval.investigation",
                        phase=unit_label(scenario.scenario_id, ready.investigation.name),
                    )
                    running = ready
            if running is not None and running.run_id is not None:
                run = await self.stack.get_run(running.run_id)
                if run["status"] != "running":
                    self._record_run(running.result, run)
                    pending.remove(running)
                elif (
                    running.run_started is not None
                    and self.clock.now() - running.run_started > RUN_TIMEOUT
                ):
                    raise Aborted("enquête sans fin au-delà de 12 min")
            if not pending:
                return
            await self.clock.sleep(POLL_S)

    def _record_run(self, result: ScenarioResult, run: Mapping[str, Any]) -> None:
        cost = run.get("total_cost_usd")
        self.spent += Decimal(str(cost)) if cost is not None else RESERVE_USD
        result.run_status = run["status"]
        result.error, result.error_type = run.get("error"), run.get("error_type")
        result.report = run.get("output")
        result.tool_calls = [
            {key: call.get(key) for key in ("tool", "input", "outcome", "response", "error")}
            for call in run.get("tool_calls", [])
        ]
        result.cost_usd = None if cost is None else float(cost)
        result.duration_ms, result.num_turns = run.get("duration_ms"), run.get("num_turns")
        result.model = run.get("model")
        if run["status"] == "succeeded":
            result.status = ScenarioStatus.COMPLETED
        elif run["status"] == "failed" and run.get("error_type") not in INFRA_ERRORS:
            result.status = ScenarioStatus.FAILED
        else:
            result.status = ScenarioStatus.INTERRUPTED
            result.reason = f"erreur d'infrastructure ({run.get('error_type') or run['status']})"
            raise InfraStop(result.reason)

    # --- Un passage ---

    def save(self) -> None:
        summary = self.report.summary
        summary.total_cost_usd = float(self.spent)
        summary.models_used = sorted({r.model for r in self.report.scenarios if r.model})
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(self.report.model_dump_json(indent=1), encoding="utf-8")
        temporary.replace(self.path)

    async def run(self, scenarios: Sequence[IncidentScenario]) -> RunStatus:
        # Toute sortie imprévue (exception, annulation) laisse le passage « interrompu »
        status = RunStatus.INTERRUPTED
        try:
            for scenario in scenarios:
                log_event(logger, "ops_eval.scenario", phase=scenario.scenario_id)
                await self.scenario(scenario)
            status = RunStatus.COMPLETED
        except BudgetExhausted:
            status = RunStatus.BUDGET_EXHAUSTED
            self.report.summary.stop_reason = "budget de l'évaluation atteint"
        except (Aborted, InfraStop, httpx.HTTPError) as exc:
            status = RunStatus.ABORTED
            self.report.summary.stop_reason = str(exc)
            for current in self.current:
                if current.status is ScenarioStatus.INTERRUPTED and current.reason is None:
                    current.reason = str(exc)
        except asyncio.CancelledError:
            self.report.summary.stop_reason = "interrompue"
            raise
        finally:
            self._finish(scenarios, status)
        return status

    def _finish(self, scenarios: Sequence[IncidentScenario], status: RunStatus) -> None:
        done = {r.scenario_id for r in self.report.scenarios}
        for scenario in scenarios:
            if scenario.scenario_id not in done:
                self.report.scenarios.extend(
                    ScenarioResult(
                        scenario_id=scenario.scenario_id,
                        investigation=investigation.name,
                        status=ScenarioStatus.NOT_RUN,
                        reason=status.value,
                    )
                    for investigation in scenario.investigations
                )
        self.report.summary.status = status
        self.report.summary.finished_at = self.clock.now()
        self.save()


# --- Pile réelle (HTTP) ------------------------------------------------------------------


class HttpStack:
    def __init__(self, token: str) -> None:
        self.token = token
        self.chaos = chaos_cli.http_sender(SHOPSIM_URL)

    async def _chaos(self, action: Callable[[], object]) -> None:
        await asyncio.to_thread(action)

    async def turn_on(self, fault: str) -> None:
        await self._chaos(
            lambda: chaos_cli.turn_on(self.chaos, self.token, chaos_cli.ChaosScenario(fault))
        )

    async def turn_off(self, fault: str) -> None:
        await self._chaos(lambda: chaos_cli.turn_off(self.chaos, self.token, fault))

    async def turn_off_all(self) -> None:
        await self._chaos(lambda: chaos_cli.turn_off(self.chaos, self.token, chaos_cli.ALL))

    async def active_faults(self) -> list[str]:
        code, body = await asyncio.to_thread(self.chaos, "GET", "/chaos", self.token)
        return [f["scenario"] for f in body["active"]] if code == 200 else ["inconnu"]

    async def _service(self, method: str, path: str, **kwargs: Any) -> Any:  # noqa: ANN401
        async with httpx.AsyncClient(base_url=SERVICE_URL, timeout=30) as client:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None

    async def firing_alerts(self) -> list[dict[str, Any]]:
        return await self._service("GET", "/alerts", params={"status": "firing", "limit": 200})

    async def post_alert(self, payload: dict[str, Any]) -> None:
        await self._service("POST", "/alerts/webhook", json=payload)

    async def loop_paused(self) -> bool:
        return bool((await self._service("GET", "/control/status"))["loop_paused"])

    async def start_run(self, alert_id: str) -> dict[str, Any]:
        return await self._service("POST", f"/runs/ops/{alert_id}")

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._service("GET", f"/runs/{run_id}")

    async def _vector(self, query: str) -> list[float]:
        async with httpx.AsyncClient(base_url=PROMETHEUS_URL, timeout=10) as client:
            response = await client.get("/api/v1/query", params={"query": query})
        values = []
        for item in response.json().get("data", {}).get("result", []):
            value = float(item["value"][1])
            if value == value:  # NaN : route sans trafic
                values.append(value)
        return values

    async def abnormal_metrics(self) -> str | None:
        p95 = await self._vector(promql("shopsim_latency_p95", "5m")["value"])
        errors = await self._vector(promql("shopsim_error_rate", "5m")["value"])
        if p95 and max(p95) > NORMAL_P95_S:
            return f"latence encore élevée (p95 {max(p95):.2f} s)"
        if errors and max(errors) > NORMAL_ERROR_RATE:
            return f"erreurs 5xx encore présentes ({max(errors):.0%})"
        return None


@dataclass
class ProcessLoad:
    process: subprocess.Popen[str]

    @property
    def pid(self) -> int | None:
        return self.process.pid

    def stop(self) -> int:
        self.process.terminate()
        try:
            out, _ = self.process.communicate(timeout=SERVICE_STOP_S)
        except subprocess.TimeoutExpired:
            self.process.kill()
            out, _ = self.process.communicate()
        try:
            return int(json.loads(out.strip().splitlines()[-1])["sent"])
        except (ValueError, IndexError, KeyError):
            return 0


def _child(parent: int) -> Callable[[], None]:
    """Sous-processus lié au lanceur : tué (SIGTERM) s'il meurt, même par `kill -9`."""
    return lambda: die_with_parent(parent, signal.SIGTERM)


def spawn(
    command: list[str], env: Mapping[str, str], stdout: int | IO[str] = subprocess.DEVNULL
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=dict(env),
        stdout=stdout,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
        preexec_fn=_child(os.getpid()),  # noqa: PLW1509  (fil principal, avant tout fil)
    )


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=SERVICE_STOP_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


# --- Ligne de commande ---------------------------------------------------------------------


def prompt_sha256() -> str:
    digest = hashlib.sha256()
    for path in (ops_agent.INSTRUCTIONS_PATH, ops_agent.GUIDE_PATH, ops_agent.SETTINGS_PATH):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def reachable(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except httpx.HTTPError:
        return False


def check_ready(environ: Mapping[str, str], probe: Callable[[str], bool] = reachable) -> str:
    """Refus avant tout lancement ; renvoie le jeton des pannes (lu dans `.env`)."""
    if environ.get("CHAOS_TOKEN"):
        raise Refused(
            "CHAOS_TOKEN est dans le shell : le service et l'agent en hériteraient. Le laisser "
            "dans .env seulement (unset CHAOS_TOKEN)."
        )
    try:
        load_api_key(environ)
    except AgentRunError as exc:
        raise Refused(str(exc)) from None
    if probe(f"{SERVICE_URL}/control/status"):
        raise Refused(
            "Le service AgentPilot tourne déjà : l'arrêter, le lanceur le démarre lui-même."
        )
    for name, url in (
        ("ShopSim", f"{SHOPSIM_URL}/health"),
        ("Prometheus", f"{PROMETHEUS_URL}/-/ready"),
        ("Alertmanager", f"{ALERTMANAGER_URL}/-/ready"),
    ):
        if not probe(url):
            raise Refused(f"{name} injoignable ({url}) : lancer `docker compose up -d`.")
    token = chaos_cli.read_token({}, ENV_FILE)
    if not token:
        raise Refused("CHAOS_TOKEN absent de .env : les pannes ne peuvent pas être activées.")
    return token


def select(spec: str | None, scenarios: Sequence[IncidentScenario]) -> list[IncidentScenario]:
    if not spec:
        return list(scenarios)
    by_id = {s.scenario_id: s for s in scenarios}
    wanted = [name.strip() for name in spec.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in by_id]
    if unknown:
        raise Refused(f"scénario inconnu : {', '.join(unknown)} (connus : {', '.join(by_id)})")
    return [by_id[name] for name in wanted]


def estimate_minutes(scenarios: Sequence[IncidentScenario]) -> float:
    """Durée prévue d'un passage : historique, pannes et enquêtes (environ 1 min chacune)."""
    return sum(s.history_min + s.end_min() + 1 + len(s.investigations) for s in scenarios)


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(prog="python -m evals.ops_run")
    found.add_argument("--scenarios", help="sous-ensemble, ex. db_errors,false_alert")
    found.add_argument("--passes", type=int, default=1, choices=range(1, 6))
    found.add_argument("--max-budget-usd", type=Decimal, default=DEFAULT_BUDGET_USD)
    found.add_argument("--name", default="ops", type=config_name)
    found.add_argument("--model", default="sonnet")
    found.add_argument("--yes", action="store_true", help="sans confirmation")
    return found


class Guard:
    """Le gardien (`evals.ops_guard`), tenu par un tube : sa fermeture sans `done` = nettoyage."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "evals.ops_guard"],
            cwd=REPO_ROOT,
            env=dict(environ),
            stdin=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def notify(self, update: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        with contextlib.suppress(BrokenPipeError):
            self.process.stdin.write(json.dumps(update) + "\n")
            self.process.stdin.flush()

    def done(self) -> None:
        self.notify({"done": True})
        assert self.process.stdin is not None
        with contextlib.suppress(BrokenPipeError):
            self.process.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def wait_ready(url: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if reachable(url):
            return
        time.sleep(1)
    raise Refused(f"le service n'a pas démarré à temps ({url})")


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    """0 : évaluation complète ; 1 : refusée ; 2 : arrêtée avant la fin."""
    args = parser().parse_args(argv)
    try:
        scenarios = select(args.scenarios, load_scenarios())
        token = check_ready(environ)
    except Refused as exc:
        print(f"Évaluation refusée : {exc}", file=sys.stderr)
        return 1
    minutes = estimate_minutes(scenarios) * args.passes
    print(
        f"{len(scenarios)} scénario(s) × {args.passes} passage(s), environ {minutes:.0f} min, "
        f"plafond {args.max_budget_usd} $"
    )
    if not args.yes and input("Lancer ? [o/N] ").strip().lower() not in {"o", "oui", "y", "yes"}:
        return 1
    configure_isolated("evals.ops_run", "ops_eval", LOG_DIR, LOG_FILE)
    signal.signal(signal.SIGTERM, _interrupt)

    from agentpilot.db import engine, service_lock
    from agentpilot.reset import reset_all

    stack = HttpStack(token)
    guard = Guard(environ)
    guard.notify({"shopsim_url": SHOPSIM_URL, "agentpilot_url": SERVICE_URL})
    service = traffic = None
    evaluation: OpsEvaluation | None = None
    status = RunStatus.INTERRUPTED
    started = datetime.now(UTC)
    series = f"{started:%Y-%m-%dT%H%M%SZ}-{args.name}"
    code = code_version()
    try:
        asyncio.run(stack.turn_off_all())
        with service_lock(engine) as locked:
            if not locked:
                raise Refused(
                    "base verrouillée : un service AgentPilot ou une remise à zéro tourne"
                )
            reset_all(engine)
        env = {**environ, "AGENTPILOT_DAILY_BUDGET_USD": str(args.max_budget_usd)}
        service = spawn(
            [str(REPO_ROOT / ".venv" / "bin" / "uvicorn"), "agentpilot.app:app", "--port", "8001"],
            env,
        )
        guard.notify({"service": service.pid})
        wait_ready(f"{SERVICE_URL}/control/status")
        traffic = spawn([sys.executable, "-m", "traffic"], environ)
        guard.notify({"traffic": traffic.pid})
        last_fault_end = datetime.now(UTC)

        def start_load(per_minute: int) -> LoadHandle:
            orders = ",".join(str(o) for o in LOAD_ORDERS)
            process = spawn(
                [
                    sys.executable,
                    "-m",
                    "evals.ops_load",
                    "--per-minute",
                    str(per_minute),
                    "--orders",
                    orders,
                ],
                environ,
                stdout=subprocess.PIPE,
            )
            return ProcessLoad(process)

        spent = Decimal(0)
        for index in range(1, args.passes + 1):
            suffix = f"-p{index}" if args.passes > 1 else ""
            path = RESULTS_DIR / f"ops-{series}{suffix}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            report = OpsEvalReport(
                summary=OpsEvalSummary(
                    name=args.name,
                    series=series,
                    pass_index=index,
                    passes=args.passes,
                    model_requested=args.model,
                    commit=code.commit,
                    dirty=code.dirty,
                    prompt_sha256=prompt_sha256(),
                    truth_sha256=truth_sha256(INCIDENTS_PATH),
                    scenarios_requested=[s.scenario_id for s in scenarios],
                    max_budget_usd=float(args.max_budget_usd),
                    started_at=datetime.now(UTC),
                )
            )
            evaluation = OpsEvaluation(
                stack,
                start_load,
                RealClock(),
                report,
                path,
                max_budget=args.max_budget_usd,
                last_fault_end=last_fault_end,
                notify=guard.notify,
                spent_before=spent,
            )
            print(f"Passage {index} : {path}")
            status = asyncio.run(evaluation.run(scenarios))
            spent += evaluation.spent
            last_fault_end = evaluation.last_fault_end
            for result in report.scenarios:
                label = unit_label(result.scenario_id, result.investigation)
                print(f"  {label:<36} {result.status.value} {result.reason or ''}")
            if status is not RunStatus.COMPLETED:
                break
    except Refused as exc:
        print(f"Évaluation refusée : {exc}", file=sys.stderr)
        status = RunStatus.ABORTED
    except KeyboardInterrupt:
        print("Interruption : nettoyage en cours…", file=sys.stderr)
        status = RunStatus.INTERRUPTED
    finally:
        # Toujours : charge, pannes, fausse alerte, puis processus
        if evaluation is not None:
            with contextlib.suppress(Exception):
                asyncio.run(evaluation.wind_down())
                if evaluation.report.summary.status is RunStatus.INTERRUPTED:
                    evaluation.save()
        with contextlib.suppress(Exception):
            asyncio.run(stack.turn_off_all())
        stop_process(traffic)
        stop_process(service)
        guard.done()
    print(f"État : {status.value}")
    return 0 if status is RunStatus.COMPLETED else 2


if __name__ == "__main__":
    sys.exit(main())
