"""Schéma du fichier de résultats d'une évaluation (`evals/results/<date>-<nom>.json`).

Écrit par `python -m evals.run` (J5.1), relu par la notation (J5.2). Ce fichier ne contient
jamais la vérité terrain : seulement ce que l'agent a décidé et ce qui s'est réellement passé
(journal des appels d'outils, actions en attente, état du ticket relu dans ShopSim).
"""

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentpilot.schemas import SupportDecision
from agentpilot.sdk_runner import INFRA_ERRORS


class EvalStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    # Ctrl+C : exécutions en cours annulées, tickets restants non lancés
    INTERRUPTED = "interrupted"
    # ShopSim injoignable ou vérification d'isolement en échec : la mesure n'a plus de sens
    ABORTED = "aborted"
    # Plafond de l'évaluation atteint : tickets restants non lancés
    BUDGET_EXHAUSTED = "budget_exhausted"


class TicketStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    NOT_RUN = "not_run"


class NotRunReason(StrEnum):
    BUDGET = "budget"
    INTERRUPTED = "interrupted"
    ABORTED = "aborted"


# Fichier d'avant la distinction des erreurs d'infrastructure : erreur de l'API reconnue au
# texte du résultat de l'agent
OLD_API_ERROR = re.compile(r"^L'agent a échoué : API Error: \d{3}\b")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionCall(Strict):
    """Appel d'un outil d'action par l'agent, tel que le journal l'a enregistré."""

    # Nom court : reply_to_ticket, refund_order, resend_order, escalate_ticket, resolve_ticket
    tool: str
    params: dict[str, Any]
    # succeeded, failed, held (mis en attente d'un humain) ou denied (refusé par le hook)
    outcome: str
    error: str | None = None


class ReadCall(Strict):
    """Lecture d'un outil par l'agent (get_order…), avec la réponse gardée au journal.

    Sert à vérifier que chaque fait cité dans `decision.facts_used` vient d'une vraie réponse
    d'outil. Réponse tronquée au-delà de 4 000 caractères (`agentpilot.hooks.clip`).
    """

    tool: str
    params: dict[str, Any]
    # succeeded ou failed
    outcome: str
    response: Any = None


class HeldAction(Strict):
    """Action sensible mise en attente d'un humain ; personne ne l'approuve en évaluation."""

    tool: str
    params: dict[str, Any]
    # over_threshold, risky_customer
    reasons: list[str]
    # Ce que le hook a lu lui-même dans ShopSim pour décider
    facts: dict[str, Any]
    status: str


class TicketResult(Strict):
    ticket_id: int
    status: TicketStatus
    not_run_reason: NotRunReason | None = None
    run_id: uuid.UUID | None = None
    trace_id: str | None = None
    # Décision validée de l'agent ; null si l'exécution a échoué
    decision: SupportDecision | None = None
    error: str | None = None
    error_type: str | None = None
    # Ce qui s'est réellement passé
    actions: list[ActionCall] = []
    held: list[HeldAction] = []
    # Lectures d'outils ; null dans un fichier d'avant J5.2 (non enregistrées)
    reads: list[ReadCall] | None = None
    # Messages réellement envoyés au client (appels reply_to_ticket réussis)
    replies: list[str] = []
    # Statut du ticket relu dans ShopSim après l'exécution ; null si illisible
    ticket_after: str | None = None
    tool_calls: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    model: str | None = None
    # Arrêt d'urgence pendant l'exécution : la décision n'est qu'une proposition
    propose_only: bool = False


def infra_failure(ticket: TicketResult) -> bool:
    """Ticket en échec à cause de l'infrastructure (API Anthropic, ShopSim, configuration, CLI),
    jamais une faute de l'agent ; reconnu aussi dans un fichier d'avant cette distinction."""
    if ticket.error_type in INFRA_ERRORS:
        return True
    return ticket.error is not None and OLD_API_ERROR.match(ticket.error) is not None


class CodeVersion(Strict):
    commit: str | None
    # Modifications non commitées au lancement
    dirty: bool | None
    # Empreinte de la consigne de l'agent (CLAUDE.md et politique de remboursement)
    prompt_sha256: str


class IsolationCheck(Strict):
    """Vérification d'isolement lancée avant le premier ticket (`sdk_runner.check_context`)."""

    passed: bool
    run_id: uuid.UUID
    trace_id: str
    error: str | None = None


class EvalSummary(Strict):
    name: str
    model_requested: str
    # Version de la consigne (`agentpilot.support_agent.PROMPTS`) ; absente avant J5.3, où seule
    # la v1 existait (la version par défaut est la v2 depuis)
    prompt_version: str = "v1"
    # Modèles annoncés par l'agent à l'initialisation
    models_used: list[str] = []
    code: CodeVersion
    simulation_now: datetime
    started_at: datetime
    finished_at: datetime | None = None
    status: EvalStatus
    # Motif d'un arrêt avant la fin (abandon, isolement en échec)
    stop_reason: str | None = None
    tickets_requested: list[int]
    max_budget_usd: float
    # Coûts connus, et plafond d'une exécution pour un coût inconnu (comme le budget du jour)
    total_cost_usd: float = 0.0
    runs_by_status: dict[str, int] = {}
    isolation: IsolationCheck | None = None
    # Aucune panne Chaos active à la fin ; null si la vérification a échoué
    chaos_clean_at_end: bool | None = None
    # Passages répétés (`--passes`) : identifiant commun de la série, rang de ce passage
    series: str | None = None
    pass_index: int = 1
    passes: int = 1


class EvalReport(Strict):
    summary: EvalSummary
    tickets: list[TicketResult] = []
