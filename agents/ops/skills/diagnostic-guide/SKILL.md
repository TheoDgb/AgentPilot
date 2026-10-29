---
name: diagnostic-guide
description: Guide de diagnostic de l'astreinte de Lumen, familles de pannes et leurs symptômes
---

# Guide de diagnostic

Chaque famille de pannes ci-dessous correspond à une cause du rapport (entre parenthèses, la
valeur à donner). Pour chacune : ses symptômes typiques, ce qui la confirme, ce qui l'écarte.
Une panne réelle ne coche pas toujours toutes les cases : cite les preuves que tu as.

## Base de données en erreur (`database_errors`)

- **Symptômes** : erreurs 5xx sur les routes qui lisent ou écrivent les données touchées,
  avec des exceptions de base de données dans les logs (connexion fermée, erreur
  opérationnelle, délai de requête). Les durées restent souvent normales : l'erreur arrive
  vite.
- **Confirme** : `http.exception` avec un type d'erreur de base de données
  (`summarize_logs`, `search_logs` niveau `error`), taux de 5xx élevé sur ces routes
  (`query_metrics shopsim_error_rate`), et seulement sur les routes qui touchent ces
  données.
- **Écarte** : aucune exception de base ; les 5xx viennent d'un autre type d'erreur ; toutes
  les routes ralentissent sans erreur.
- **Attention** : la santé de ShopSim peut rester verte.
- **Correctif à proposer** : vérifier l'état de la base (connexions, disponibilité, journaux
  du serveur PostgreSQL), redémarrer ou basculer si besoin.

## Saturation de ressources (`resource_saturation`)

- **Symptômes** : la latence monte progressivement, sur toutes les routes qui passent par la
  ressource partagée (connexions, file d'attente, processeur), jusqu'à un plafond. Peu ou pas
  d'erreurs au début ; des délais dépassés peuvent suivre.
- **Confirme** : tendance de la p95 qui monte par paliers (`query_metrics
  shopsim_latency_trend`), même ordre de grandeur sur toutes les routes métier, alors que les
  routes qui n'utilisent pas la ressource (santé, horloge) restent rapides.
- **Écarte** : une seule route touchée ; un saut d'un coup sans montée ; des erreurs 5xx
  franches dès le début.
- **Correctif à proposer** : identifier la ressource épuisée (pool de connexions, requêtes
  lentes), augmenter sa capacité ou réduire la charge, redémarrer le service si la ressource
  ne se libère pas.

## Dépendance ou service lent (`slow_dependency`)

- **Symptômes** : une route (ou les seules routes d'un même service) devient lente d'un coup,
  souvent à une valeur stable, sans erreurs ; les autres routes restent normales.
- **Confirme** : p95 élevée sur cette seule route (`summarize_logs`, `query_metrics
  shopsim_latency_p95`), saut net dans la tendance plutôt qu'une montée, autres routes
  inchangées.
- **Écarte** : toutes les routes métier touchées de la même façon ; latence qui monte
  progressivement.
- **Correctif à proposer** : vérifier le service ou le fournisseur dont dépend la route, ses
  délais et sa disponibilité ; prévoir un délai d'attente et un repli.

## Erreur applicative ou déploiement fautif (`application_error`)

- **Symptômes** : erreurs 5xx sur une fonction précise (souvent une écriture), avec des
  exceptions applicatives (clé de configuration manquante, erreur de type, bogue), pas de
  base de données ; le reste fonctionne.
- **Confirme** : `http.exception` avec un type d'erreur applicatif sur les seules routes
  touchées ; début net, comme après une mise en production.
- **Écarte** : exceptions de base de données ; latence en cause plutôt qu'erreurs.
- **Correctif à proposer** : revenir à la version précédente ou corriger la configuration
  manquante, puis vérifier la route.

## Service injoignable (`service_unreachable`)

- **Symptômes** : un service ne répond plus du tout : santé `down`, cible absente des
  métriques (`targets_up` à 0), plus aucune ligne de log récente pour lui, outils qui
  renvoient « injoignable ».
- **Confirme** : `check_health` en `down`, `query_metrics targets_up` proche de 0 sur la
  fenêtre.
- **Écarte** : le service répond, même lentement ou avec des erreurs.
- **Correctif à proposer** : redémarrer le service, vérifier son hôte et son réseau.

## Problème de la plateforme d'agents (`agent_platform`)

- **Symptômes** : la boutique va bien, mais AgentPilot signale un problème : exécutions de
  l'agent Support en échec, budget du jour presque atteint, action qui attend un humain
  depuis longtemps, état illisible.
- **Confirme** : `query_metrics agentpilot_runs` (échecs), `agentpilot_state` (budget, actions
  en attente, état), logs `agentpilot` en avertissement ou erreur ; routes de ShopSim
  normales.
- **Écarte** : erreurs ou lenteurs dans ShopSim qui expliquent les échecs de l'agent.
- **Correctif à proposer** : selon le cas, traiter les actions en attente, relever le budget,
  vérifier la clé ou la limite de dépense de l'API, la base de la plateforme.

## Aucune panne (`no_incident`)

- **Symptômes** : l'alerte s'est déclenchée, mais rien d'anormal ne se voit maintenant ni
  pendant sa période : pic passager, seuil trop sensible, alerte déjà résolue.
- **Confirme** : taux de 5xx et latences normaux sur la route de l'alerte et ailleurs, santé
  verte, pas d'exception ; preuves à l'appui, jamais par défaut.
- **Écarte** : le moindre symptôme durable correspondant à l'alerte.
- **Correctif à proposer** : aucun sur le système ; éventuellement revoir le seuil de
  l'alerte.

## Indéterminée (`undetermined`)

- Quand les preuves ne départagent pas les hypothèses, ou qu'une source manque : dis-le,
  donne une confiance moyenne ou faible, et indique ce qu'il faudrait vérifier pour trancher.
