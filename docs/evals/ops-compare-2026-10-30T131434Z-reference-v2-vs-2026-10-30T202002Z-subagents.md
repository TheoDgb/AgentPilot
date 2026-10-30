# Comparaison de l'agent Ops : 2026-10-30T131434Z-reference-v2 contre 2026-10-30T202002Z-subagents

- Base : `2026-10-30T131434Z-reference-v2`, architecture `solo`, consigne `9704eed2dc32`, 1 passage(s)
- Candidate : `2026-10-30T202002Z-subagents`, architecture `subagents`, consigne `c298c7325064`, 1 passage(s)
- Renotées le 2026-10-30 23:28 UTC par `python -m evals.ops_compare`, version de la notation `11a2215b0643`, vérité terrain `1d0c1c3e153b`

## Verdict

**Candidate pas retenue** : 1 régression(s).

Règle fixée avant l'essai : retenue seulement sans régression (aucune enquête réussie par la base et ratée par la candidate, aucune faute grave ni preuve introuvable de plus) **et** moins chère ou plus rapide, sur les enquêtes notées des deux côtés. Ce verdict n'est qu'un calcul : la décision reste humaine.

## Score et preuves

| | Base | Candidate |
|---|---|---|
| Enquêtes réussies | 12 / 12 (fautes graves : 0) | 11 / 12 (fautes graves : 0) |
| Preuves exactes / retrouvées / invérifiables / introuvables | 142 / 0 / 0 / 0 | 177 / 13 / 5 / 0 |

## Coût, durée, tours et appels

Totaux sur les enquêtes notées des deux côtés (moyenne des passages par enquête). Les tours ne comptent que ceux de l'agent principal : avec des subagents, les appels aux outils de lecture sont la mesure comparable.

| | Base | Candidate | Écart |
|---|---|---|---|
| Coût | 0,7801 <span>$</span> | 1,7522 <span>$</span> | +125 % |
| Durée des enquêtes | 201 s | 387 s | +93 % |
| Tours (agent principal) | 88 | 64 | -27 % |
| Appels aux outils de lecture | 64 | 97 | +52 % |

| Enquête | Coût base | Coût candidate | Durée base | Durée candidate | Appels base | Appels candidate |
|---|---|---|---|---|---|---|
| `db_errors` | 0,0645 <span>$</span> | 0,1542 <span>$</span> | 16 s | 33 s | 5 | 11 |
| `payment_latency` | 0,0654 <span>$</span> | 0,1356 <span>$</span> | 17 s | 27 s | 5 | 8 |
| `saturation` | 0,0685 <span>$</span> | 0,1390 <span>$</span> | 20 s | 29 s | 4 | 8 |
| `bad_deploy` | 0,0605 <span>$</span> | 0,1445 <span>$</span> | 13 s | 30 s | 5 | 7 |
| `saturation_then_payments` | 0,0667 <span>$</span> | 0,1454 <span>$</span> | 17 s | 34 s | 6 | 8 |
| `false_alert` | 0,0820 <span>$</span> | 0,1395 <span>$</span> | 19 s | 32 s | 8 | 7 |
| `db_and_payments · orders` | 0,0650 <span>$</span> | 0,1524 <span>$</span> | 17 s | 32 s | 5 | 9 |
| `db_and_payments · payments` | 0,0444 <span>$</span> | 0,1252 <span>$</span> | 14 s | 31 s | 4 | 8 |
| `db_and_bad_deploy` | 0,0658 <span>$</span> | 0,1510 <span>$</span> | 17 s | 31 s | 5 | 8 |
| `db_intermittent` | 0,0622 <span>$</span> | 0,1405 <span>$</span> | 15 s | 30 s | 5 | 7 |
| `saturation_then_bad_deploy` | 0,0651 <span>$</span> | 0,1471 <span>$</span> | 19 s | 33 s | 6 | 8 |
| `db_errors_ended` | 0,0701 <span>$</span> | 0,1780 <span>$</span> | 18 s | 47 s | 6 | 8 |

Appels de la candidate par agent (tous passages) : logs-investigator 33, metrics-investigator 49, principal 15.

## Régressions

| Enquête | Base | Candidate | Détail |
|---|---|---|---|
| `saturation_then_payments` | 100 % (slow_dependency) | 0 % (resource_saturation) | taux de réussite en baisse; en échec : cause, routes |

## Améliorations

Aucune.

## Limites

- Pas de test de signe : avec un passage et une base déjà à 100 %, il ne dirait rien. Une différence d'une enquête peut venir du hasard.
- Les durées dépendent aussi de l'API au moment de chaque série.
