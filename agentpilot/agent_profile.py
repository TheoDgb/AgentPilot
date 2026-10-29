"""Profil d'un agent lancé par le SDK (`sdk_runner`) : ce qui distingue Support et Ops.

L'isolement est le même pour tous (`sdk_runner.build_options`) ; le profil donne la consigne,
le serveur MCP et ses outils, et les plafonds d'une exécution.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentProfile:
    # Nom du serveur MCP (préfixe des outils `mcp__<nom>__…`) et sa configuration stdio
    mcp_server: str
    mcp_config: dict[str, Any]
    # Seuls outils autorisés (et attendus à l'initialisation), en plus de la sortie structurée
    tool_names: frozenset[str]
    system_prompt: str
    max_turns: int
    max_budget_usd: float
    timeout_s: float
