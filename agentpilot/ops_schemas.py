"""Schémas Pydantic des sorties de l'agent Ops : rapport d'incident et vérification d'isolement.

Les causes forment une liste fermée de familles génériques (`agents/ops/skills/
diagnostic-guide/SKILL.md`), jamais des noms de pannes simulées : l'évaluation (J6.3) fera
la correspondance de son côté.
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IncidentCause(StrEnum):
    DATABASE_ERRORS = "database_errors"
    RESOURCE_SATURATION = "resource_saturation"
    SLOW_DEPENDENCY = "slow_dependency"
    APPLICATION_ERROR = "application_error"
    SERVICE_UNREACHABLE = "service_unreachable"
    AGENT_PLATFORM = "agent_platform"
    NO_INCIDENT = "no_incident"
    UNDETERMINED = "undetermined"


# Causes qui touchent forcément des routes de ShopSim : au moins une route dans le rapport.
# `service_unreachable` et `agent_platform` peuvent n'en toucher aucune ; `undetermined` admet
# les deux cas ; `no_incident` n'en touche aucune
ROUTE_CAUSES = frozenset(
    {
        IncidentCause.DATABASE_ERRORS,
        IncidentCause.RESOURCE_SATURATION,
        IncidentCause.SLOW_DEPENDENCY,
        IncidentCause.APPLICATION_ERROR,
    }
)


class HypothesisStatus(StrEnum):
    CONFIRMED = "confirmed"
    RULED_OUT = "ruled_out"
    INCONCLUSIVE = "inconclusive"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


OpsTool = Literal["search_logs", "summarize_logs", "query_metrics", "list_alerts", "check_health"]


class Evidence(BaseModel):
    """Une valeur lue dans un outil : l'outil, ses arguments, le champ et la valeur exacte."""

    model_config = ConfigDict(extra="forbid")

    tool: OpsTool = Field(description="Outil appelé, ex. summarize_logs")
    arguments: dict[str, str | int | None] = Field(
        description='Arguments de l\'appel, ex. {"service": "shopsim", "since_minutes": 5}'
    )
    field: str = Field(
        min_length=1,
        max_length=200,
        description="Champ lu dans la réponse, ex. requests.routes[/orders/{order_id}].5xx",
    )
    value: str | int | float | bool | None = Field(description="Valeur exacte lue")
    observed: str | None = Field(
        default=None, max_length=60, description="Âge de la valeur (ago) ou now de la réponse"
    )


class Symptom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=300)
    routes: list[str] = Field(default_factory=list, description="Gabarits de route concernés")
    evidence: list[Evidence] = Field(min_length=1)


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: IncidentCause
    status: HypothesisStatus
    reasoning: str = Field(min_length=1, max_length=600)
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decided_with_evidence(self) -> Self:
        if self.status is not HypothesisStatus.INCONCLUSIVE and not self.evidence:
            raise ValueError(
                f"hypothèse {self.cause.value} {self.status.value} sans preuve : une hypothèse "
                "confirmée ou écartée cite au moins une preuve"
            )
        return self


class IncidentReport(BaseModel):
    """Rapport d'incident de l'agent Ops, sur une alerte enregistrée par AgentPilot."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(description="Identifiant de l'alerte de départ, recopié")
    alertname: str = Field(description="Nom de l'alerte, recopié")
    alert_route: str | None = Field(description="Route de l'alerte, recopiée, ou null")
    summary: str = Field(min_length=1, max_length=300, description="L'incident en une phrase")
    symptoms: list[Symptom] = Field(min_length=1, max_length=10)
    hypotheses: list[Hypothesis] = Field(min_length=2, max_length=6)
    probable_cause: IncidentCause
    confidence: Confidence
    affected_routes: list[str] = Field(description="Gabarits de route touchés")
    proposed_fix: str = Field(
        min_length=1, max_length=600, description="Ce qu'un humain devrait faire (jamais fait)"
    )
    next_checks: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        causes = [h.cause for h in self.hypotheses]
        if len(set(causes)) != len(causes):
            raise ValueError("deux hypothèses portent sur la même cause")
        confirmed = [h.cause for h in self.hypotheses if h.status is HypothesisStatus.CONFIRMED]
        if self.probable_cause is IncidentCause.UNDETERMINED:
            if confirmed:
                raise ValueError("cause indéterminée : aucune hypothèse ne peut être confirmée")
            if self.confidence is Confidence.HIGH:
                raise ValueError("cause indéterminée : la confiance ne peut pas être élevée")
        elif confirmed != [self.probable_cause]:
            raise ValueError(
                "la cause probable doit être la seule hypothèse confirmée "
                f"(confirmées : {[c.value for c in confirmed]})"
            )
        if not any(h.status is HypothesisStatus.RULED_OUT for h in self.hypotheses):
            raise ValueError("au moins une hypothèse doit être écartée, preuve à l'appui")
        if self.probable_cause is IncidentCause.NO_INCIDENT and self.affected_routes:
            raise ValueError("aucune panne : aucune route touchée")
        if self.probable_cause in ROUTE_CAUSES and not self.affected_routes:
            raise ValueError(f"cause {self.probable_cause.value} : au moins une route touchée")
        return self

    def cited_tools(self) -> set[str]:
        """Outils cités dans les preuves (symptômes et hypothèses)."""
        evidence = [e for s in self.symptoms for e in s.evidence]
        evidence += [e for h in self.hypotheses for e in h.evidence]
        return {e.tool for e in evidence}


class OpsContextCheck(BaseModel):
    """Vérification d'isolement de l'agent Ops : ce que sa consigne dit, et rien d'autre."""

    model_config = ConfigDict(extra="forbid")

    causes: list[str] = Field(description="Valeurs de la liste fermée des causes du rapport")
    confidence_levels: list[str] = Field(description="Valeurs possibles du niveau de confiance")
    tools: list[str] = Field(description="Noms courts des outils d'enquête de la consigne")
    launched_at: str = Field(description="Heure de lancement donnée par la consigne")
    fault_token_header: str | None = Field(
        description="En-tête HTTP du jeton des pannes simulées, ou null si inconnu"
    )
    fault_log_file: str | None = Field(
        description="Fichier de logs des pannes simulées, ou null si inconnu"
    )
    incidents_ground_truth_path: str | None = Field(
        description="Chemin de la vérité terrain des incidents, ou null si inconnu"
    )
    tickets_ground_truth_path: str | None = Field(
        description="Chemin de la vérité terrain des tickets, ou null si inconnu"
    )
    postgres_host_port: str | None = Field(
        description="Port PostgreSQL de ShopSim sur l'hôte, ou null si inconnu"
    )
