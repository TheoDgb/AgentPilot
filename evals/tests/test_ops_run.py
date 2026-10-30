"""Lanceur de l'évaluation de l'agent Ops (`evals.ops_run`), son gardien et sa charge, sans
appel réel : horloge simulée, pile simulée (pannes, alertes, service, Prometheus)."""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from evals import ops_guard, ops_run
from evals.incident_truth import load_scenarios
from evals.ops_load import Load
from evals.ops_results import OpsEvalReport, OpsEvalSummary, RunStatus, ScenarioStatus
from evals.ops_run import OpsEvaluation, Refused, check_ready, pick_alert, select

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
SCENARIOS = {s.scenario_id: s for s in load_scenarios()}
RUN_ID = "651f924c-94f8-4b91-8118-0217f8790a10"
REPORT_OK = {"probable_cause": "database_errors", "confidence": "high"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeClock:
    def __init__(self) -> None:
        self.current = T0

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        await asyncio.sleep(0)


@dataclass
class Rule:
    """Une alerte qui se déclenche `delay_min` après l'activation d'une de ses pannes et reste
    la même occurrence tant qu'une de ses pannes est active."""

    faults: set[str]
    alertname: str
    route: str | None
    delay_min: float
    pending_since: datetime | None = None
    open: dict[str, Any] | None = None


@dataclass
class FakeStack:
    clock: FakeClock
    rules: list[Rule] = field(default_factory=list)
    events: list[tuple[Any, ...]] = field(default_factory=list)
    active: dict[str, datetime] = field(default_factory=dict)
    occurrences: list[dict[str, Any]] = field(default_factory=list)
    injected: dict[str, dict[str, Any]] = field(default_factory=dict)
    loop: bool = True
    abnormal: list[str] = field(default_factory=list)
    run_polls: int = 1
    run: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "succeeded",
            "output": REPORT_OK,
            "error": None,
            "error_type": None,
            "total_cost_usd": "0.06",
            "duration_ms": 19000,
            "num_turns": 6,
            "model": "claude-sonnet-5-5",
            "tool_calls": [
                {
                    "tool": "mcp__ops__check_health",
                    "input": {},
                    "outcome": "succeeded",
                    "response": {},
                    "seq": 1,
                }
            ],
        }
    )
    fail_alerts_after: int | None = None
    _polls: int = 0
    _alert_calls: int = 0

    async def turn_on(self, fault: str) -> None:
        self.events.append(("on", fault))
        self.active[fault] = self.clock.now()

    async def turn_off(self, fault: str) -> None:
        self.events.append(("off", fault))
        self.active.pop(fault, None)

    async def turn_off_all(self) -> None:
        self.events.append(("off_all",))
        self.active.clear()

    async def active_faults(self) -> list[str]:
        return list(self.active)

    def _update(self) -> None:
        now = self.clock.now()
        for rule in self.rules:
            on = any(f in self.active for f in rule.faults)
            if not on:
                rule.pending_since = None
                if rule.open is not None:
                    rule.open = None
                continue
            if rule.open is None:
                rule.pending_since = rule.pending_since or now
                if now - rule.pending_since >= timedelta(minutes=rule.delay_min):
                    rule.open = {
                        "id": str(uuid.uuid4()),
                        "alertname": rule.alertname,
                        "route": rule.route,
                        "starts_at": now.isoformat(),
                    }
                    self.occurrences.append(rule.open)

    async def firing_alerts(self) -> list[dict[str, Any]]:
        self._alert_calls += 1
        if self.fail_alerts_after is not None and self._alert_calls > self.fail_alerts_after:
            raise RuntimeError("panne du faux service")
        self._update()
        firing = [r.open for r in self.rules if r.open is not None]
        return firing + list(self.injected.values())

    async def post_alert(self, payload: dict[str, Any]) -> None:
        alert = payload["alerts"][0]
        self.events.append(("alert", payload["status"]))
        if payload["status"] == "firing":
            self.injected[alert["fingerprint"]] = {
                "id": str(uuid.uuid4()),
                "alertname": alert["labels"]["alertname"],
                "route": alert["labels"].get("route"),
                "starts_at": alert["startsAt"],
            }
        else:
            self.injected.pop(alert["fingerprint"], None)

    async def loop_paused(self) -> bool:
        return self.loop

    async def start_run(self, alert_id: str) -> dict[str, Any]:
        self.events.append(("run", alert_id))
        return {"id": RUN_ID, "trace_id": "a" * 32, "status": "running"}

    async def get_run(self, run_id: str) -> dict[str, Any]:
        self._polls += 1
        if self._polls <= self.run_polls:
            return {"status": "running"}
        return self.run

    async def abnormal_metrics(self) -> str | None:
        return self.abnormal.pop(0) if self.abnormal else None


@dataclass
class FakeLoad:
    stack: FakeStack
    pid: int | None = 4242

    def stop(self) -> int:
        self.stack.events.append(("load_stop",))
        return 7


def evaluation(
    stack: FakeStack,
    tmp_path: Path,
    *,
    max_budget: str = "5",
    last_fault_end: datetime | None = None,
    notify: Any = None,  # noqa: ANN401
) -> OpsEvaluation:
    report = OpsEvalReport(
        summary=OpsEvalSummary(
            name="test",
            series="s",
            model_requested="sonnet",
            prompt_sha256="p",
            truth_sha256="t",
            scenarios_requested=[],
            max_budget_usd=float(max_budget),
            started_at=T0,
        )
    )

    def start_load(per_minute: int) -> FakeLoad:
        stack.events.append(("load_start", per_minute))
        return FakeLoad(stack)

    return OpsEvaluation(
        stack,
        start_load,
        stack.clock,
        report,
        tmp_path / "ops.json",
        max_budget=Decimal(max_budget),
        last_fault_end=last_fault_end or T0 - timedelta(hours=1),
        notify=notify or (lambda update: None),
    )


def db_rule() -> Rule:
    return Rule({"db_errors"}, "ShopSimHighErrorRate", "/orders/{order_id}", 2.5)


async def test_db_errors_scenario_end_to_end(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(clock, [db_rule()])
    run = evaluation(stack, tmp_path)

    status = await run.run([SCENARIOS["db_errors"]])

    assert status is RunStatus.COMPLETED
    [found] = run.report.scenarios
    assert found.status is ScenarioStatus.COMPLETED
    assert found.report == REPORT_OK and found.cost_usd == 0.06
    assert found.tool_calls == [
        {
            "tool": "mcp__ops__check_health",
            "input": {},
            "outcome": "succeeded",
            "response": {},
            "error": None,
        }
    ]
    assert str(found.alert_id) == stack.occurrences[0]["id"]
    # Enquête lancée à 3,5 min (alerte née à 2,5 min), panne arrêtée après
    started = next(i for i, e in enumerate(stack.events) if e[0] == "run")
    assert stack.events[0] == ("on", "db_errors")
    assert stack.events[-1] == ("off_all",)
    assert started < len(stack.events) - 1
    assert [f.fault for f in found.faults] == ["db_errors"] and found.faults[0].stopped_at
    assert run.spent == Decimal("0.06")
    saved = OpsEvalReport.model_validate_json((tmp_path / "ops.json").read_text())
    assert saved.summary.status is RunStatus.COMPLETED


async def test_waits_for_a_clean_state(tmp_path: Path) -> None:
    """Panne active (désactivée), historique trop court, latence encore élevée : il attend."""
    clock = FakeClock()
    stack = FakeStack(clock, [db_rule()], abnormal=["latence encore élevée"] * 3)
    stack.active["payment_latency"] = T0
    run = evaluation(stack, tmp_path, last_fault_end=T0 - timedelta(minutes=2))

    await run.wait_clean(SCENARIOS["db_errors"])

    assert stack.events[0] == ("off_all",)
    # history_min = 6 depuis la fin de la panne trouvée active (à T0)
    assert clock.now() - T0 >= timedelta(minutes=6)


async def test_clean_state_never_comes(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(clock, abnormal=["erreurs 5xx"] * 1000)
    run = evaluation(stack, tmp_path)

    status = await run.run([SCENARIOS["db_errors"], SCENARIOS["false_alert"]])

    assert status is RunStatus.ABORTED
    assert "état propre introuvable" in (run.report.summary.stop_reason or "")
    assert [s.status for s in run.report.scenarios] == [
        ScenarioStatus.INTERRUPTED,
        ScenarioStatus.NOT_RUN,
    ]


async def test_chain_investigates_the_occurrence_born_during_saturation(tmp_path: Path) -> None:
    """Saturation puis paiements lents : l'alerte des paiements née pendant la saturation reste
    la même occurrence ; c'est elle qui est confiée à l'agent, à 18 min."""
    clock = FakeClock()
    stack = FakeStack(
        clock,
        [
            Rule(
                {"connection_saturation", "payment_latency"},
                "ShopSimHighLatency",
                "/payments/{order_id}",
                5,
            )
        ],
    )
    run = evaluation(stack, tmp_path)

    await run.run([SCENARIOS["saturation_then_payments"]])

    [found] = run.report.scenarios
    [occurrence] = stack.occurrences
    assert str(found.alert_id) == occurrence["id"]
    assert [e for e in stack.events if e[0] in ("on", "off")] == [
        ("on", "connection_saturation"),
        ("off", "connection_saturation"),
        ("on", "payment_latency"),
    ]
    assert [f.fault for f in found.faults] == ["connection_saturation", "payment_latency"]
    assert found.faults[0].stopped_at is not None
    run_at = next(i for i, e in enumerate(stack.events) if e[0] == "run")
    assert stack.events.index(("on", "payment_latency")) < run_at


def test_pick_alert_rejects_an_occurrence_born_after_the_change_of_cause() -> None:
    scenario = SCENARIOS["saturation_then_payments"]
    late = {
        "id": "late",
        "alertname": "ShopSimHighLatency",
        "route": "/payments/{order_id}",
        "starts_at": (T0 + timedelta(minutes=13)).isoformat(),
    }
    early = late | {"id": "early", "starts_at": (T0 + timedelta(minutes=5)).isoformat()}
    old = late | {"id": "old", "starts_at": (T0 - timedelta(minutes=30)).isoformat()}

    assert pick_alert([late], scenario.alert, scenario, T0) is None
    assert pick_alert([old], scenario.alert, scenario, T0) is None
    assert pick_alert([late, early], scenario.alert, scenario, T0)["id"] == "early"  # type: ignore[index]


async def test_missing_alert_is_no_alert_and_the_series_goes_on(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(clock)
    run = evaluation(stack, tmp_path)

    status = await run.run([SCENARIOS["db_errors"], SCENARIOS["false_alert"]])

    assert status is RunStatus.COMPLETED
    first, second = run.report.scenarios
    assert first.status is ScenarioStatus.NO_ALERT and "absente" in (first.reason or "")
    assert second.status is ScenarioStatus.COMPLETED
    assert ("off_all",) in stack.events


async def test_false_alert_is_posted_then_resolved(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(clock)
    updates: list[dict[str, Any]] = []
    run = evaluation(stack, tmp_path, notify=updates.append)

    await run.run([SCENARIOS["false_alert"]])

    [found] = run.report.scenarios
    assert found.status is ScenarioStatus.COMPLETED and found.faults == []
    alerts = [e for e in stack.events if e[0] == "alert"]
    assert alerts == [("alert", "firing"), ("alert", "resolved")]
    assert stack.injected == {}
    # Le gardien connaît la fausse alerte tant qu'elle est active
    assert updates[0]["fake_alert"]["status"] == "resolved" and updates[-1] == {"fake_alert": None}


async def test_budget_stops_before_the_investigation(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(clock, [db_rule()])
    run = evaluation(stack, tmp_path, max_budget="0.5")

    status = await run.run([SCENARIOS["db_errors"], SCENARIOS["false_alert"]])

    assert status is RunStatus.BUDGET_EXHAUSTED
    assert [(s.status, s.reason) for s in run.report.scenarios] == [
        (ScenarioStatus.NOT_RUN, "budget"),
        (ScenarioStatus.NOT_RUN, "budget_exhausted"),
    ]
    assert not any(e[0] == "run" for e in stack.events)
    assert stack.active == {}


@pytest.mark.parametrize("error_type", ["api_unavailable", "config", "sdk_error"])
async def test_infrastructure_error_stops_the_series(tmp_path: Path, error_type: str) -> None:
    clock = FakeClock()
    stack = FakeStack(clock, [db_rule()])
    stack.run = stack.run | {
        "status": "failed",
        "output": None,
        "error": "API",
        "error_type": error_type,
    }
    run = evaluation(stack, tmp_path)

    status = await run.run([SCENARIOS["db_errors"], SCENARIOS["false_alert"]])

    assert status is RunStatus.ABORTED
    first, second = run.report.scenarios
    assert first.status is ScenarioStatus.INTERRUPTED and error_type in (first.reason or "")
    assert second.status is ScenarioStatus.NOT_RUN
    assert stack.active == {}


async def test_agent_failure_is_graded_and_the_series_goes_on(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(clock, [db_rule()])
    stack.run = stack.run | {
        "status": "failed",
        "output": None,
        "error": "Rapport invalide",
        "error_type": "invalid_output",
    }
    run = evaluation(stack, tmp_path)

    status = await run.run([SCENARIOS["db_errors"]])

    assert status is RunStatus.COMPLETED
    assert run.report.scenarios[0].status is ScenarioStatus.FAILED


async def test_support_loop_running_aborts(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(clock, [db_rule()], loop=False)
    run = evaluation(stack, tmp_path)

    status = await run.run([SCENARIOS["db_errors"]])

    assert status is RunStatus.ABORTED
    assert "boucle de l'agent Support" in (run.report.summary.stop_reason or "")


# --- Charge de remboursements : toujours arrêtée avant la panne ----------------------------


def order_of(events: list[tuple[Any, ...]], *names: str) -> list[int]:
    return [next(i for i, e in enumerate(events) if e[0] == name) for name in names]


async def test_load_is_stopped_before_the_fault_at_the_end(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(clock, [Rule({"bad_deploy"}, "ShopSimHighErrorRate", "/refunds", 2.5)])
    run = evaluation(stack, tmp_path)

    await run.run([SCENARIOS["bad_deploy"]])

    started, stopped, off = order_of(stack.events, "load_start", "load_stop", "off_all")
    assert started < stopped < off
    assert ("on", "bad_deploy") == stack.events[stack.events.index(("load_start", 12)) - 1]
    assert run.report.scenarios[0].refunds_sent == 7


async def test_load_is_stopped_before_its_fault_when_the_step_ends(tmp_path: Path) -> None:
    scenario = SCENARIOS["bad_deploy"].model_copy(
        update={
            "steps": [SCENARIOS["bad_deploy"].steps[0].model_copy(update={"stop_min": 2.0})],
            "investigate_after_min": 3.5,
        }
    )
    clock = FakeClock()
    stack = FakeStack(clock, [Rule({"bad_deploy"}, "ShopSimHighErrorRate", "/refunds", 1)])
    run = evaluation(stack, tmp_path)

    await run.run([scenario])

    stopped, off = order_of(stack.events, "load_stop", "off")
    assert stopped < off


async def test_load_is_stopped_before_the_fault_on_an_exception(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(
        clock, [Rule({"bad_deploy"}, "ShopSimHighErrorRate", "/refunds", 2.5)], fail_alerts_after=4
    )
    run = evaluation(stack, tmp_path)

    with pytest.raises(RuntimeError):
        await run.run([SCENARIOS["bad_deploy"]])

    stopped, off = order_of(stack.events, "load_stop", "off_all")
    assert stopped < off
    assert run.report.summary.status is RunStatus.INTERRUPTED


async def test_load_is_stopped_before_the_fault_on_ctrl_c(tmp_path: Path) -> None:
    clock = FakeClock()
    stack = FakeStack(clock, [Rule({"bad_deploy"}, "ShopSimHighErrorRate", "/refunds", 2.5)])
    run = evaluation(stack, tmp_path)
    task = asyncio.create_task(run.run([SCENARIOS["bad_deploy"]]))
    while ("load_start", 12) not in stack.events:
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stopped, off = order_of(stack.events, "load_stop", "off_all")
    assert stopped < off
    assert run.report.summary.status is RunStatus.INTERRUPTED
    assert run.report.scenarios[0].status is ScenarioStatus.INTERRUPTED


# --- Gardien ------------------------------------------------------------------------------


def test_guard_cleans_up_in_order() -> None:
    state = ops_guard.read_state(
        [
            json.dumps({"service": 11, "traffic": 12}),
            json.dumps({"load": 13, "fake_alert": {"status": "resolved"}}),
            "pas du json",
        ]
    )
    calls: list[str] = []

    steps = ops_guard.cleanup(
        state,
        stop=lambda pid: calls.append(f"stop {pid}"),
        disable_faults=lambda url: calls.append("faults"),
        resolve=lambda url, payload: calls.append("alert"),
    )

    assert calls == ["stop 13", "faults", "alert", "stop 12", "stop 11"]
    assert steps == ["load", "faults", "alert", "traffic", "service"]


def test_guard_keeps_going_after_a_failing_step() -> None:
    state = ops_guard.read_state([json.dumps({"service": 11})])
    calls: list[str] = []

    def broken(url: str) -> None:
        raise OSError("ShopSim injoignable")

    steps = ops_guard.cleanup(state, stop=lambda pid: calls.append("stop"), disable_faults=broken)

    assert steps == ["faults (échec)", "service"] and calls == ["stop"]


def test_guard_does_nothing_when_the_launcher_finished() -> None:
    assert ops_guard.read_state([json.dumps({"service": 11}), json.dumps({"done": True})]).done


def test_real_guard_stops_the_processes_when_its_pipe_closes(tmp_path: Path) -> None:
    """Le lanceur meurt (tube fermé sans `done`) : le gardien arrête les processus connus."""
    sleepers = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) for _ in range(2)
    ]
    env = {**os.environ, "SHOPSIM_URL": "http://127.0.0.1:9"}
    guard = subprocess.Popen(
        [sys.executable, "-m", "evals.ops_guard"], stdin=subprocess.PIPE, text=True, env=env
    )
    assert guard.stdin is not None
    guard.stdin.write(
        json.dumps(
            {
                "traffic": sleepers[0].pid,
                "service": sleepers[1].pid,
                "shopsim_url": "http://127.0.0.1:9",
            }
        )
        + "\n"
    )
    guard.stdin.close()

    guard.wait(timeout=30)
    for sleeper in sleepers:
        assert sleeper.wait(timeout=10) is not None


# --- Charge : l'envoi en cours va au bout, plus aucun ensuite ------------------------------


def test_load_finishes_the_send_in_flight_then_stops() -> None:
    sent: list[str] = []

    def send(method: str, path: str, body: Any, headers: Any) -> int:  # noqa: ANN401
        if method == "GET":
            return 7
        sent.append(path)
        load.stop()  # le signal arrive pendant l'envoi
        return 500

    load = Load(send, per_minute=60, orders=[1, 2])

    assert load.run(sleep=lambda s: None) == 1
    assert sent == ["/refunds"]


def test_load_skips_orders_without_payment() -> None:
    calls: list[str] = []

    def send(method: str, path: str, body: Any, headers: Any) -> int:  # noqa: ANN401
        calls.append(f"{method} {path}")
        if len(calls) > 3:
            load.stop()
        return 0 if method == "GET" else 500

    load = Load(send, per_minute=60, orders=[1])

    assert load.run(sleep=lambda s: None) == 0
    assert all(c.startswith("GET") for c in calls)


# --- Refus avant tout lancement ------------------------------------------------------------


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    path.write_text("CHAOS_TOKEN=secret\n")
    monkeypatch.setattr(ops_run, "ENV_FILE", path)
    return path


@pytest.mark.parametrize(
    ("environ", "up", "message"),
    [
        ({"CHAOS_TOKEN": "x", "ANTHROPIC_API_KEY": "k"}, set(), "CHAOS_TOKEN est dans le shell"),
        ({"ANTHROPIC_API_KEY": "k"}, {"control"}, "tourne déjà"),
        ({"ANTHROPIC_API_KEY": "k"}, set(), "ShopSim injoignable"),
        ({"ANTHROPIC_API_KEY": "k"}, {"health"}, "Prometheus injoignable"),
    ],
)
def test_refusals(env_file: Path, environ: dict[str, str], up: set[str], message: str) -> None:
    with pytest.raises(Refused, match=message):
        check_ready(environ, probe=lambda url: any(part in url for part in up))


def test_ready(env_file: Path) -> None:
    up = ("health", "ready")
    token = check_ready({"ANTHROPIC_API_KEY": "k"}, probe=lambda url: any(p in url for p in up))

    assert token == "secret"


def test_unknown_scenario_is_refused() -> None:
    with pytest.raises(Refused, match="scénario inconnu"):
        select("db_errors,cosmic_rays", list(SCENARIOS.values()))
    assert [s.scenario_id for s in select("false_alert,db_errors", list(SCENARIOS.values()))] == [
        "false_alert",
        "db_errors",
    ]


def test_launcher_never_writes_where_the_agent_reads(tmp_path: Path) -> None:
    from observability import logs

    logs.configure_isolated("evals.ops_run", "ops_eval", tmp_path, ops_run.LOG_FILE)
    logs.log_event(ops_run.logger, "ops_eval.scenario", phase="db_errors")

    assert [p.name for p in tmp_path.iterdir()] == ["ops_eval.jsonl"]
    assert "db_errors" in (tmp_path / "ops_eval.jsonl").read_text()
