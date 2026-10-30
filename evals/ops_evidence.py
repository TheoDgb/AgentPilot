"""Recoupement des preuves d'un rapport de l'agent Ops avec les vraies réponses d'outils (J6.3).

Chaque preuve cite un outil, ses arguments, un champ (`field`) et la valeur lue. Elle est
recoupée avec les appels gardés au journal de l'exécution (réponses complètes, voir
`agentpilot.hooks.OPS_MAX_RESPONSE_CHARS`) :

1. appels du même outil dont les arguments s'accordent avec ceux de la preuve : chaque argument
   cité par la preuve a la même valeur dans l'appel (valeurs par défaut des outils comprises) ;
   un argument omis par la preuve (`limit`…) n'est pas comparé ;
2. chemin `field` lu avec tolérance : segments séparés par des points, `[clé]` qui choisit dans
   une liste l'élément dont un champ d'identité (`route`, `service`, `event`, `labels.route`…)
   vaut la clé, ou un index ; préfixe `requests.` facultatif ;
3. valeur comparée : nombres égaux, à l'arrondi choisi par l'agent près ; textes identiques.

Verdicts : `exact` (chemin résolu, même valeur) ; `found` (chemin non résolu, mais valeur
présente dans la réponse) ; `unverifiable` (chemin non résolu et valeur banale, qu'on trouve
partout : `0`, `1`, `true`, `false`, `null`, chaîne vide ; jamais validée, jamais une faute) ;
`fabricated` (valeur différente au chemin résolu, ou absente de la réponse, ou aucun appel avec
ces arguments).
"""

import inspect
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mcp_server.ops.logs_tools import LOG_TOOLS
from mcp_server.ops.metrics_tools import METRIC_TOOLS
from mcp_server.ops.status_tools import STATUS_TOOLS

TOOL_PREFIX = "mcp__ops__"
# Champs qui identifient un élément de liste dans les réponses des outils Ops
IDENTITY_FIELDS = ("route", "service", "event", "error_type", "alertname", "metric")
LABEL_FIELDS = ("route", "job", "status", "state")
_TOKEN = re.compile(r"([^.\[\]]+)|\[([^\]]*)\]")


class EvidenceVerdict(StrEnum):
    EXACT = "exact"
    FOUND = "found"
    UNVERIFIABLE = "unverifiable"
    FABRICATED = "fabricated"


@dataclass(frozen=True)
class EvidenceCheck:
    tool: str
    field: str
    value: Any
    verdict: EvidenceVerdict
    reason: str = ""


def _defaults() -> dict[str, dict[str, Any]]:
    """Valeurs par défaut des arguments de chaque outil Ops, lues sur leur signature."""
    found = {}
    for tool in [*LOG_TOOLS, *METRIC_TOOLS, *STATUS_TOOLS]:
        found[tool.__name__] = {
            name: param.default
            for name, param in inspect.signature(tool).parameters.items()
            if param.default is not inspect.Parameter.empty
        }
    return found


DEFAULTS = _defaults()


def _normalized(
    tool: str, arguments: Mapping[str, Any], with_defaults: bool = True
) -> dict[str, Any]:
    defaults = DEFAULTS.get(tool, {})
    values = dict(defaults) if with_defaults else {}
    for key, value in arguments.items():
        if isinstance(value, str) and value.isdigit() and isinstance(defaults.get(key), int):
            value = int(value)
        values[key] = value
    return values


def _agrees(stated: Mapping[str, Any], call: Mapping[str, Any]) -> bool:
    """Chaque argument cité par la preuve a la même valeur dans l'appel."""
    return all(call.get(key) == value for key, value in stated.items())


def is_trivial(value: Any) -> bool:  # noqa: ANN401
    """Valeur banale, qu'on trouve partout dans une réponse : jamais validée sans chemin."""
    if value is None or value == "":
        return True
    if isinstance(value, bool):
        return True
    return isinstance(value, int | float) and value in (0, 1)


def _decimals(number: float) -> int:
    text = repr(float(number))
    return len(text.split(".")[1]) if "." in text and "e" not in text else 0


def same_value(cited: Any, actual: Any) -> bool:  # noqa: ANN401
    """Même valeur : nombres égaux ou arrondis par l'agent (`0.935` pour `0.9352`), textes et
    booléens identiques. `True` n'est jamais égal à `1`."""
    if isinstance(cited, bool) or isinstance(actual, bool):
        return type(cited) is type(actual) and cited == actual
    if isinstance(cited, int | float) and isinstance(actual, int | float):
        if float(cited) == float(actual):
            return True
        return round(float(actual), _decimals(cited)) == float(cited)
    if isinstance(cited, str) and isinstance(actual, str):
        return cited.strip() == actual.strip()
    return cited is None and actual is None


def _tokens(field: str) -> list[str]:
    return [name or key for name, key in _TOKEN.findall(field)]


def _identity(item: Any, key: str) -> bool:  # noqa: ANN401
    if not isinstance(item, dict):
        return False
    if any(str(item.get(name)) == key for name in IDENTITY_FIELDS if name in item):
        return True
    labels = item.get("labels")
    return isinstance(labels, dict) and any(
        str(labels.get(name)) == key for name in LABEL_FIELDS if name in labels
    )


def _step(node: Any, token: str) -> list[Any]:  # noqa: ANN401
    """Nœuds atteints depuis `node` par un segment (plusieurs si la clé est ambiguë)."""
    if isinstance(node, list):
        if token.isdigit() and int(token) < len(node):
            return [node[int(token)]]
        return [item for item in node if _identity(item, token)]
    if isinstance(node, dict):
        if token in node:
            return [node[token]]
        # Préfixe omis : `routes[...]` pour `requests.routes[...]`
        nested = node.get("requests")
        if isinstance(nested, dict) and token in nested:
            return [nested[token]]
    return []


def resolve(response: Any, field: str) -> list[Any]:  # noqa: ANN401
    """Valeurs au bout du chemin `field` (vide si le chemin ne mène nulle part)."""
    nodes = [response]
    for token in _tokens(field):
        nodes = [found for node in nodes for found in _step(node, token)]
        if not nodes:
            return []
    return nodes


def _leaves(node: Any) -> Iterable[Any]:  # noqa: ANN401
    if isinstance(node, dict):
        for value in node.values():
            yield from _leaves(value)
    elif isinstance(node, list):
        for value in node:
            yield from _leaves(value)
    else:
        yield node


def check(evidence: Mapping[str, Any], calls: Sequence[Mapping[str, Any]]) -> EvidenceCheck:
    """Verdict d'une preuve d'après les appels de l'exécution (`tool_calls` de `GET /runs`)."""
    tool, field, value = evidence["tool"], evidence["field"], evidence.get("value")
    stated = _normalized(tool, evidence.get("arguments") or {}, with_defaults=False)
    responses = [
        call.get("response")
        for call in calls
        if call.get("tool") == f"{TOOL_PREFIX}{tool}"
        and call.get("outcome") == "succeeded"
        and _agrees(stated, _normalized(tool, call.get("input") or {}))
    ]

    def verdict(kind: EvidenceVerdict, reason: str = "") -> EvidenceCheck:
        return EvidenceCheck(tool, field, value, kind, reason)

    if not responses:
        return verdict(EvidenceVerdict.FABRICATED, "aucun appel de cet outil avec ces arguments")
    resolved = [found for response in responses for found in resolve(response, field)]
    if resolved:
        if any(same_value(value, actual) for actual in resolved):
            return verdict(EvidenceVerdict.EXACT)
        return verdict(EvidenceVerdict.FABRICATED, f"valeur lue au chemin : {resolved[0]!r}")
    if is_trivial(value):
        return verdict(EvidenceVerdict.UNVERIFIABLE, "chemin introuvable, valeur banale")
    if any(same_value(value, leaf) for response in responses for leaf in _leaves(response)):
        return verdict(EvidenceVerdict.FOUND, "chemin introuvable, valeur présente")
    return verdict(EvidenceVerdict.FABRICATED, "valeur absente de la réponse")


def report_evidence(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Toutes les preuves d'un rapport (symptômes puis hypothèses)."""
    evidence = [e for s in report.get("symptoms", []) for e in s.get("evidence", [])]
    return evidence + [e for h in report.get("hypotheses", []) for e in h.get("evidence", [])]
