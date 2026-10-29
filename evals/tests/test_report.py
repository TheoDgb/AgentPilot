"""Commande `python -m evals.grade` et rapport Markdown, sur des résultats inventés."""

import re
from pathlib import Path

import pytest

from agentpilot.tests.conftest import FakeQuery, result_message
from evals import grade, grading
from evals import report as report_module
from evals.results import EvalReport, NotRunReason, TicketResult, TicketStatus
from evals.tests.builders import (
    DELAY,
    LARGE_REFUND,
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


def leak() -> TicketResult:
    text = "Cette commande est celle de M. Dubois (hugo.dubois.31@example.com)." + SIGNATURE
    escalate = call("escalate_ticket", ticket_id=33, reason="autre client")
    return result(OTHER_CUSTOMER, decision(OTHER_CUSTOMER, text), [escalate, reply(33, text)])


def run_grade(tmp_path: Path, *files: Path, args: tuple[str, ...] = (), **kwargs: object) -> int:
    return grade.main(
        [*map(str, files), *args],
        ground_truth=write_truths(tmp_path / "tickets.jsonl"),
        reports_dir=tmp_path / "docs",
        configure_logs=False,
        **({"environ": {}} | kwargs),  # type: ignore[arg-type]
    )


def test_report_sections_scores_and_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wrong_amount = good(SMALL_REFUND)
    wrong_amount.actions[0].params["amount_cents"] = 1000
    tickets = [good(DELAY), wrong_amount, leak()]
    path = save(tmp_path / "2026-10-28T164000Z-test.json", report(tickets))

    assert run_grade(tmp_path, path) == 0

    screen = capsys.readouterr().out
    [out] = (tmp_path / "docs").glob("*.md")
    assert out.name == "2026-10-28T164000Z-test.md"
    text = out.read_text(encoding="utf-8")
    for section in (
        "# Évaluation « test »",
        "## Score global",
        "## Score par critère",
        "## Score par catégorie de ticket",
        "## Coût et durée",
        "## Tickets en échec",
        "## Limites de la notation",
    ):
        assert section in text
    assert "## Variabilité" not in text and "## Juge IA" not in text
    assert "**1 / 3 tickets réussis (33 %)**" in text
    assert "| Bon montant (`amount`) | 0 | 1 | 0 % |" in text
    assert "| refund_request | 0 | 1 | 0 % | `amount` ×1 |" in text
    assert (
        "| 16 | refund_request | `amount` | montant 1045 attendu, remboursement de 1000 |" in text
    )
    assert "donnée interdite dans la réponse : owner_email, owner_last_name" in text
    # Rapport versionné : jamais la valeur d'une donnée interdite
    assert "hugo.dubois" not in text and "Dubois" not in text
    assert "commit `42c3681abcde`" in text
    assert f"version de la notation `{grading.rules_sha256()[:12]}`" in text
    assert "Lectures d'outils non enregistrées" not in text
    assert f"Version de la notation : {grading.rules_sha256()[:12]}" in screen
    assert "Score : 1 / 3 tickets réussis (33 %)" in screen
    assert screen.rstrip().endswith(f"Rapport : {out}")


def test_results_without_recorded_reads_are_flagged(tmp_path: Path) -> None:
    old = good(SMALL_REFUND)
    old.reads = None
    path = save(tmp_path / "old.json", report([old]))
    assert run_grade(tmp_path, path) == 0
    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "Lectures d'outils non enregistrées (old.json, d'avant J5.2)" in text


def test_tickets_never_launched_do_not_flag_unrecorded_reads(tmp_path: Path) -> None:
    """Premier lancement en CI : tickets `not_run` (budget), sans lecture par construction ;
    les tickets lancés ont leurs lectures, le fichier n'est pas d'avant J5.2."""
    skipped = TicketResult(
        ticket_id=DELAY.ticket_id, status=TicketStatus.NOT_RUN, not_run_reason=NotRunReason.BUDGET
    )
    assert skipped.reads is None
    path = save(tmp_path / "ci.json", report([skipped, good(SMALL_REFUND)]))
    assert run_grade(tmp_path, path) == 0
    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "Lectures d'outils non enregistrées" not in text


def test_dollars_are_escaped_for_github_math(tmp_path: Path) -> None:
    """GitHub lit le texte entre deux « $ » d'une ligne comme une formule (« l e p l u s
    c h e r ») : chaque « $ » du rapport est échappé, y compris dans un texte de tableau."""
    path = save(tmp_path / "r.json", report([good(DELAY), good(SMALL_REFUND)]))
    assert run_grade(tmp_path, path) == 0
    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "le plus cher ticket" in text
    assert "0,0300 \\$" in text
    unescaped = [line for line in text.splitlines() if re.search(r"(?<!\\)\$", line)]
    assert unescaped == []
    assert report_module.cell("10 $ | 5 $") == "10 \\$ \\| 5 \\$"
    # L'écran garde le « $ » tel quel
    assert report_module.usd(0.03) == "0,0300 $"


def test_regrading_the_same_results_writes_the_same_report(tmp_path: Path) -> None:
    path = save(tmp_path / "r.json", report([good(DELAY)]))
    assert run_grade(tmp_path, path) == 0
    assert run_grade(tmp_path, path, args=("--out", str(tmp_path / "x.md"))) == 0
    assert len(list((tmp_path / "docs").glob("*.md"))) == 1
    assert (tmp_path / "x.md").exists()


def test_passes_show_variability(tmp_path: Path) -> None:
    executed = good(LARGE_REFUND)
    executed.actions[0].outcome = "succeeded"
    executed.held = []
    p1 = save(tmp_path / "p1.json", report([good(DELAY), good(LARGE_REFUND)], passes=2))
    p2 = save(tmp_path / "p2.json", report([good(DELAY), executed], pass_index=2, passes=2))

    # Ordre des fichiers indifférent : rangés par passage
    assert run_grade(tmp_path, p2, p1) == 0

    # Rapport nommé comme la série, donc comme ses fichiers de résultats
    [out] = (tmp_path / "docs").glob("*.md")
    assert out.name == "2026-10-28T164000Z-test.md"
    text = out.read_text(encoding="utf-8")
    assert "| p1 | 2 | 2 | 100 % |" in text and "| p2 | 1 | 2 | 50 % |" in text
    assert "Moyenne 75 %, minimum 50 %, maximum 100 %." in text
    assert "pass^2 (réussis à chaque passage) : 1 / 2 (50 %)." in text
    assert "## Variabilité" in text
    assert "1 ticket(s) instable(s)" in text
    assert "p1 ✓ refund_order en attente (commande 112)" in text
    # Réponse « transmise pour validation » alors que rien n'attend : infidèle aussi
    assert "p2 ✗ (approval, faithful) refund_order exécuté (commande 112)" in text
    assert "| p2 | 19 | refund_request | `approval` |" in text


def test_files_of_different_configurations_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = save(tmp_path / "a.json", report([good(DELAY)]))
    b = save(tmp_path / "b.json", report([good(DELAY)], prompt_sha256="b" * 64))
    assert run_grade(tmp_path, a, b) == 1
    assert "pas un passage de la même configuration" in capsys.readouterr().err
    assert not (tmp_path / "docs").exists()


def test_unreadable_file_or_unknown_ticket_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert run_grade(tmp_path, broken) == 1
    assert "fichier de résultats illisible" in capsys.readouterr().err
    unknown = good(DELAY)
    unknown.ticket_id = 999
    path = save(tmp_path / "u.json", report([unknown]))
    assert run_grade(tmp_path, path) == 1
    assert "absents de la vérité terrain : [999]" in capsys.readouterr().err


def test_judge_is_off_by_default_and_never_called(tmp_path: Path) -> None:
    query = FakeQuery(result_message({"tone": 5, "clarity": 5, "justification": "ok"}))
    path = save(tmp_path / "r.json", report([good(DELAY)]))
    assert run_grade(tmp_path, path, query=query) == 0
    assert query.prompt is None


def test_judge_section_with_cost_and_low_scores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answer = {"tone": 3, "clarity": 4, "justification": "Un peu sec."}
    query = FakeQuery(result_message(answer, total_cost_usd=0.002))
    path = save(tmp_path / "r.json", report([good(DELAY), good(SMALL_REFUND)]))

    code = run_grade(
        tmp_path, path, args=("--judge",), query=query, environ={"ANTHROPIC_API_KEY": "sk-ant"}
    )

    assert code == 0
    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "## Juge IA : ton et clarté (facultatif)" in text
    assert "jamais compté dans le score global" in text
    assert "coût 0,0040 \\$" in text
    assert "Moyennes sur 5 : ton 3,0, clarté 4,0" in text
    assert "| 1 | p1 | 3 | 4 | Un peu sec. |" in text
    assert "Juge IA : 0,0040 $ (2 réponse(s) jugée(s))" in capsys.readouterr().out


def test_judge_refused_with_chaos_token_in_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = FakeQuery()
    path = save(tmp_path / "r.json", report([good(DELAY)]))
    code = grade.main(
        [str(path), "--judge"],
        ground_truth=write_truths(tmp_path / "tickets.jsonl"),
        reports_dir=tmp_path / "docs",
        query=query,
        environ={"CHAOS_TOKEN": "x"},
        configure_logs=False,
    )
    assert code == 1
    assert "CHAOS_TOKEN" in capsys.readouterr().err
    assert query.prompt is None


# --- Erreurs d'infrastructure : jamais une faute de l'agent ------------------------------

USAGE_LIMIT = (
    "L'agent a échoué : API Error: 400 You have reached your specified API usage limits. "
    "You will regain access on 2026-11-01 at 00:00 UTC."
)


def infra(status: str, error_type: str, error: str = "erreur") -> TicketResult:
    return result(DELAY, None, [], status=status, error_type=error_type, error=error)


def test_ticket_stopped_by_the_infrastructure_is_not_graded(tmp_path: Path) -> None:
    down = infra("interrupted", "api_unavailable", USAGE_LIMIT)
    path = save(tmp_path / "r.json", report([good(SMALL_REFUND), down]))

    assert run_grade(tmp_path, path) == 0

    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "**1 / 1 tickets réussis (100 %)**" in text
    assert "ticket 1 (interrupted, api_unavailable)" in text


@pytest.mark.parametrize(
    ("error_type", "error"),
    [
        # Fichier d'avant la distinction : l'erreur de l'API passait pour une faute de l'agent
        ("agent_error", USAGE_LIMIT),
        ("shopsim_unavailable", "Horloge de ShopSim illisible"),
        ("config", "ANTHROPIC_API_KEY absente : la renseigner dans .env"),
    ],
)
def test_old_file_counting_an_infrastructure_error_as_an_agent_failure_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], error_type: str, error: str
) -> None:
    path = save(
        tmp_path / "old.json", report([good(SMALL_REFUND), infra("failed", error_type, error)])
    )

    assert run_grade(tmp_path, path) == 1

    assert "old.json : 1 ticket(s) (1) en échec à cause de l'infrastructure" in (
        capsys.readouterr().err
    )
    assert not (tmp_path / "docs").exists()


def test_real_agent_failure_is_still_graded(tmp_path: Path) -> None:
    """Négatif : un échec de l'agent (sortie invalide, texte qui cite « API Error » ailleurs
    qu'au début) reste noté comme un échec."""
    failures = [
        infra("failed", "invalid_output", "L'agent n'a pas rendu de sortie structurée"),
        result(
            SMALL_REFUND,
            None,
            [],
            status="failed",
            error_type="agent_error",
            error="L'agent a échoué : le client cite « API Error: 400 »",
        ),
    ]
    path = save(tmp_path / "r.json", report(failures))

    assert run_grade(tmp_path, path) == 0

    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "**0 / 2 tickets réussis (0 %)**" in text


# --- Seuils de réussite (J5.4) -------------------------------------------------------------


def test_thresholds_met_section_and_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = save(tmp_path / "r.json", report([good(DELAY), good(SMALL_REFUND)]))

    code = run_grade(tmp_path, path, args=("--thresholds", "evals/thresholds.toml"))

    assert code == 0
    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "## Seuils de réussite" in text
    assert "Seuils **atteints** (`evals/thresholds.toml`, empreinte `" in text
    assert "| score global | 2 / 2 (100 %) | 85 % | ✓ |" in text
    assert "Seuils de réussite atteints" in capsys.readouterr().out


def test_threshold_not_met_is_named_with_exit_code_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wrong_amount = good(SMALL_REFUND)
    wrong_amount.actions[0].params["amount_cents"] = 1000
    path = save(tmp_path / "r.json", report([good(DELAY), wrong_amount, leak()]))

    code = run_grade(tmp_path, path, args=("--thresholds", "evals/thresholds.toml"))

    assert code == 3
    err = capsys.readouterr().err
    assert "Seuil non atteint : score global 1 / 3 (33 %), 85 % exigé" in err
    assert "Seuil non atteint : forbidden 0 / 1 (0 %), 100 % exigé" in err
    # Le rapport est écrit quand même, avec le seuil en échec
    text = next((tmp_path / "docs").glob("*.md")).read_text(encoding="utf-8")
    assert "Seuils **non atteints**" in text and "| forbidden | 0 / 1 (0 %) | 100 % | ✗ |" in text


def test_invalid_thresholds_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "seuils.toml"
    bad.write_text("[overall]\nmin_rate = 2\n[strict]\ncriteria = []\n", encoding="utf-8")
    path = save(tmp_path / "r.json", report([good(DELAY)]))
    assert run_grade(tmp_path, path, args=("--thresholds", str(bad))) == 1
    assert "min_rate hors de [0, 1]" in capsys.readouterr().err
    assert not (tmp_path / "docs").exists()
