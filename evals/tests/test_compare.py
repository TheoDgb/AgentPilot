"""Commande `python -m evals.compare` et son rapport, sur des séries inventées."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from agentpilot.tests.conftest import FakeQuery, result_message
from evals import compare
from evals.compare_report import signed
from evals.results import EvalReport, EvalStatus
from evals.tests.builders import (
    DELAY,
    OTHER_CUSTOMER,
    SIGNATURE,
    SMALL_REFUND,
    call,
    decision,
    good,
    reply,
    report,
    result,
    write_truths,
)


def save(path: Path, data: EvalReport) -> Path:
    path.write_text(data.model_dump_json(), encoding="utf-8")
    return path


def leak() -> Any:  # noqa: ANN401
    text = "Cette commande est celle de M. Dubois (hugo.dubois.31@example.com)." + SIGNATURE
    escalate = call("escalate_ticket", ticket_id=33, reason="autre client")
    return result(OTHER_CUSTOMER, decision(OTHER_CUSTOMER, text), [escalate, reply(33, text)])


def wrong_amount() -> Any:  # noqa: ANN401
    bad = good(SMALL_REFUND)
    bad.actions[0].params["amount_cents"] = 1000
    return bad


def base(tmp_path: Path, tickets: list[Any] | None = None, **changes: Any) -> Path:  # noqa: ANN401
    data = report(tickets or [good(DELAY), wrong_amount(), leak()], name="baseline")
    for key, value in changes.items():
        setattr(data.summary, key, value)
    return save(tmp_path / "2026-10-28T224853Z-baseline.json", data)


def candidate(tmp_path: Path, tickets: list[Any] | None = None, **changes: Any) -> Path:  # noqa: ANN401
    tickets = tickets or [good(DELAY), good(SMALL_REFUND), good(OTHER_CUSTOMER)]
    data = report(tickets, name="prompt-v2", prompt_sha256="b" * 64)
    data.summary.prompt_version = "v2"
    for key, value in changes.items():
        setattr(data.summary, key, value)
    return save(tmp_path / "2026-10-29T100000Z-prompt-v2.json", data)


def run_compare(
    tmp_path: Path, a: Path, b: Path, args: tuple[str, ...] = (), **kwargs: object
) -> int:
    return compare.main(
        ["--base", str(a), "--candidate", str(b), *args],
        ground_truth=write_truths(tmp_path / "tickets.jsonl"),
        reports_dir=tmp_path / "docs",
        configure_logs=False,
        **({"environ": {}} | kwargs),  # type: ignore[arg-type]
    )


def test_report_side_by_side(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_compare(tmp_path, base(tmp_path), candidate(tmp_path))

    assert code == 0
    [out] = (tmp_path / "docs").glob("*.md")
    assert out.name == "compare-2026-10-28T164000Z-baseline-vs-2026-10-28T164000Z-prompt-v2.md"
    text = out.read_text(encoding="utf-8")
    for section in (
        "# Comparaison « baseline » (A) et « prompt-v2 » (B)",
        "## Score global",
        "## Score par critère",
        "## Score par catégorie de ticket",
        "## Tickets améliorés et dégradés",
        "## Coût et durée",
        "## Limites de la comparaison",
    ):
        assert section in text
    assert "- Ce qui change : consigne (v1 → v2)" in text
    assert "consigne v2 (empreinte `bbbbbbbbbbbb`)" in text
    assert "renotées avec la version de la notation" in text
    assert "| Tickets réussis (tous passages) | 1 / 3 (33 %) | 3 / 3 (100 %) | +67 pts |" in text
    # 2 tickets améliorés sur 2 : trop peu pour conclure
    assert "non significatif (2 ↑ / 0 ↓, p = 0,500)" in text
    assert "### Améliorés (2)" in text and "### Dégradés (0)" in text
    assert "| 16 | refund_request | p1 ✗ (amount) | p1 ✓ | `amount` |" in text
    # Ni valeur interdite ni texte de client
    assert "hugo.dubois.31@example.com" not in text and "Dubois" not in text
    screen = capsys.readouterr().out
    assert "A : 1 / 3 (33 %) ; B : 3 / 3 (100 %)" in screen
    assert "Tickets : 2 amélioré(s), 0 dégradé(s), non significatif (p = 0,500)" in screen
    assert f"Rapport : {out}" in screen


@pytest.mark.parametrize(
    ("make_b", "message"),
    [
        (lambda p: candidate(p, [good(DELAY)]), "pas sur les mêmes tickets"),
        (lambda p: base(p).rename(p / "copie.json") or p / "copie.json", "rien à comparer"),
        (
            lambda p: candidate(p, status=EvalStatus.BUDGET_EXHAUSTED),
            "s'est arrêté avant la fin (budget_exhausted)",
        ),
    ],
)
def test_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    make_b: Any,  # noqa: ANN401
    message: str,
) -> None:
    b = make_b(tmp_path)
    a = base(tmp_path)
    assert run_compare(tmp_path, a, b) == 1
    assert message in capsys.readouterr().err
    assert not (tmp_path / "docs").exists()


class ByVersion:
    """Faux juge : ton 2 pour les réponses de A, 4 pour celles de B (reconnues au texte)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.calls += 1
        tone = 4 if "Nous avons remboursé 10,45 €" in prompt or "conseiller" in prompt else 2
        answer = {"tone": tone, "clarity": 4, "justification": "ok"}
        return FakeQuery(result_message(answer, total_cost_usd=0.002))(
            prompt=prompt, options=options
        )


def test_judge_both_series_in_the_same_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = ByVersion()
    code = run_compare(
        tmp_path,
        base(tmp_path),
        candidate(tmp_path),
        args=("--judge",),
        query=query,
        environ={"ANTHROPIC_API_KEY": "sk-ant"},
    )

    assert code == 0
    # Une réponse par ticket et par série
    assert query.calls == 6
    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "## Juge IA : ton et clarté (facultatif)" in text
    assert "Juge IA (les deux séries) : 0,0120 $." in text
    assert "| Ton | 2,7 | 3,3 | +0,7 | non significatif (1 ↑ / 0 ↓, p = 1,000) |" in text
    # Ticket 33 : réponse fautive de A, escalade de B
    assert "| 33 | 2,0 → 4,0 | 4,0 → 4,0 |" in text
    assert "Juge IA : 0,0120 $" in capsys.readouterr().out


def test_judge_shares_one_cap_between_the_series(tmp_path: Path) -> None:
    query = ByVersion()
    # 0,05 $ réservé par appel, 0,002 $ dépensé : A jugée en entier, plus rien pour B
    code = run_compare(
        tmp_path,
        base(tmp_path),
        candidate(tmp_path),
        args=("--judge", "--judge-max-usd", "0.055"),
        query=query,
        environ={"ANTHROPIC_API_KEY": "sk-ant"},
    )
    assert code == 0
    assert query.calls == 3
    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "Réponses non jugées (plafond atteint) : 3." in text


def test_judge_refused_with_chaos_token_in_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = ByVersion()
    code = run_compare(
        tmp_path,
        base(tmp_path),
        candidate(tmp_path),
        args=("--judge",),
        query=query,
        environ={"CHAOS_TOKEN": "x"},
    )
    assert code == 1 and query.calls == 0
    assert "CHAOS_TOKEN" in capsys.readouterr().err


def test_refused_when_a_series_counts_an_api_error_as_an_agent_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cas réel du 2026-10-29 : passage `completed`, tickets en échec faute d'API."""
    down = result(DELAY, None, [], status="failed", error_type="agent_error")
    down.error = (
        "L'agent a échoué : API Error: 400 You have reached your specified API usage limits."
    )
    b = candidate(tmp_path, [down, good(SMALL_REFUND), good(OTHER_CUSTOMER)])
    assert run_compare(tmp_path, base(tmp_path), b) == 1
    assert "en échec à cause de l'infrastructure" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("value", "digits", "shown"),
    [
        # Vu dans le rapport réel : écart de ton de 0,02 affiché « +0,0 »
        (0.02, 1, "0,0"),
        (-0.04, 1, "0,0"),
        (0.0, 0, "0"),
        (0.4, 0, "0"),
        (0.06, 1, "+0,1"),
        (-1.25, 1, "-1,2"),
        (6.25, 0, "+6"),
    ],
)
def test_signed_gap_never_shows_a_signed_zero(value: float, digits: int, shown: str) -> None:
    assert signed(value, digits) == shown
