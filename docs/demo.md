# Démonstration en 3 minutes

Ce que la démo montre : un remboursement retenu pour un humain puis approuvé en direct, une
panne diagnostiquée seule par l'agent Ops, la notification sur Discord, et les résultats
chiffrés, limites comprises.

**Rien ne s'attend en direct.** Une panne met environ 4 minutes à devenir une enquête terminée
(2 minutes de règle d'alerte, environ 10 secondes de regroupement des alertes, 60 secondes
d'attente avant le tri, une quinzaine de secondes d'enquête). Tout se prépare donc avant.

## Préparation, la veille

1. Dans `.env` :
   - `ANTHROPIC_API_KEY` : la clé d'API ;
   - `CHAOS_TOKEN` : un jeton au choix (vide, les pannes sont fermées) ;
   - `AGENTPILOT_DISCORD_WEBHOOK_URL` : l'adresse du webhook du salon Discord de la démo.
2. `docker compose up -d --build`
3. Une **répétition complète**, chronométrée : la préparation ci-dessous, puis le script.

## Préparation, 15 minutes avant

Commandes à lancer depuis la racine du dépôt, dans l'ordre.

1. **Remise à zéro** (boutique et historique d'AgentPilot) :
   ```sh
   docker compose stop agentpilot
   python -m agentpilot.reset --yes
   docker compose start agentpilot
   ```
2. **Vérification d'isolement** (facultative) :
   ```sh
   curl -X POST localhost:8001/checks/support
   curl -X POST localhost:8001/checks/ops
   ```
3. **Générateur de trafic**, toujours **avant** la panne : au lancement, il relève les
   commandes des clients, ce qui échoue si la panne est déjà active. Attendre 30 secondes.
   ```sh
   docker compose --profile traffic up -d --force-recreate traffic
   ```
4. **T − 10 min : l'agent Support sur le ticket 20** (remboursement de 264,95 €). La boucle
   reste en pause : seule cette exécution part. Vérifier qu'une action attend sur
   http://localhost:8001/approvals.
   ```sh
   curl -X POST localhost:8001/runs/support/20
   ```
5. **T − 6 min : la panne.** Activer l'enquête automatique sur http://localhost:8001/control
   (section « Enquête automatique », avec un nom), puis :
   ```sh
   python -m shopsim.chaos_cli on db_errors
   ```
6. **T − 1 min : tout est prêt** si l'alerte est sur `/control`, l'enquête terminée sur
   `/investigations`, et deux messages sur Discord (l'alerte, puis la fin de l'enquête).
7. **Onglets ouverts, dans l'ordre de la démo** :
   1. http://localhost:8001/control
   2. l'action en attente (`/approvals`, puis le ticket 20)
   3. Grafana : http://localhost:3000/d/agentpilot-overview
   4. l'enquête (`/investigations`, puis la plus récente)
   5. le salon Discord
   6. la section « Résultats » du [README](../README.md#résultats)

## Le script

| Temps | Ce qu'on montre | Ce qu'on dit | Clic |
|---|---|---|---|
| 0:00–0:30 | `/control` : boucle en pause, budget du jour, alerte active | « Deux agents d'IA travaillent dans une boutique simulée. Démarrer ne coûte rien : la boucle est en pause tant qu'un humain ne la relance pas, et le budget du jour est plafonné. Une panne est en cours, on y revient. » | Onglet 1 |
| 0:30–1:20 | `/approvals`, le ticket 20 | « Le client demande 264,95 €. L'agent a décidé de rembourser, mais c'est le code, pas l'IA, qui a retenu l'action : au-dessus de 50 €, un humain valide. Voici les faits relus dans la boutique, et la réponse déjà envoyée au client, qui n'annonce rien de faux. J'approuve. » Puis, sur `/control` : « Ce bouton coupe toutes les actions d'un coup : l'agent ne fait plus que proposer. » | Onglet 2, **Approuver et exécuter** ; montrer le bouton d'arrêt d'urgence sans l'activer |
| 1:20–2:20 | Grafana, puis l'enquête, puis Discord | « La panne se voit dans les mesures : des erreurs sur les pages de commandes. L'agent Ops a enquêté seul, en lecture seule : cause probable, erreurs de base de données. Chaque preuve cite la valeur exacte lue, ici le nombre d'erreurs, retrouvable dans le journal des appels. Il a écarté d'autres causes, et propose un correctif qu'il n'exécute jamais. L'équipe a été prévenue sur Discord. » | Onglets 3, 4 et 5 |
| 2:20–3:00 | Le README, section « Résultats » | « Tout est mesuré. L'agent Support réussit 91 tickets sur 96, mais l'écart avec l'ancienne consigne n'est pas significatif, et je le dis. L'agent Ops a réussi les 12 enquêtes de référence, sur un seul passage. Et une limite : les pages n'ont pas d'authentification, le service reste en local. » | Onglet 6 |

Chiffres du script : [rapport v2](evals/2026-10-29T021314Z-prompt-v2-notation-8e959d276b3f.md),
[comparaison v1 contre v2](evals/compare-2026-10-28T224853Z-baseline-vs-2026-10-29T021314Z-prompt-v2-notation-8e959d276b3f.md),
[référence de l'agent Ops](evals/ops-2026-10-30T131434Z-reference-v2.md).

## Après la démo

```sh
python -m shopsim.chaos_cli off all
docker compose stop traffic
```

Puis couper l'enquête automatique sur `/control`.

## Si l'API ne répond pas

La démo reste montrable : la panne d'IA devient un garde-fou à montrer.

- **Agent Ops** : l'enquête échoue sur une erreur d'infrastructure, et un **rapport sans IA**
  apparaît de lui-même sur `/investigations`, sans coût. Il donne les faits (pages en erreur,
  types d'erreurs, alertes actives) mais jamais de cause. L'alerte `OpsReportWithoutAI`
  arrive sur Discord environ une minute plus tard. À dire : « Sans IA, le système ne devine
  pas : il donne les faits et prévient un humain. »
- **Agent Support** : la boucle passe en pause « erreur d'infrastructure », visible sur
  `/control`, et aucun ticket n'est compté en échec. Si l'action du ticket 20 n'a pas pu être
  préparée, montrer la [capture de la page d'approbation](images/approvals.png).
- **Répéter ce cas** : mettre une fausse clé dans `.env`, puis
  `docker compose up -d agentpilot`, et suivre la préparation. Remettre la vraie clé ensuite.

## Coût de la démo

| Étape | Coût | Source |
|---|---|---|
| L'agent Support sur le ticket 20 | 0,02 à 0,05 <span>$</span> (par ticket : moyenne 0,0215 <span>$</span>, maximum 0,0464 <span>$</span>) | [rapport v2](evals/2026-10-29T021314Z-prompt-v2-notation-8e959d276b3f.md) |
| L'enquête de l'agent Ops | 0,04 à 0,08 <span>$</span> | [référence de l'agent Ops](evals/ops-2026-10-30T131434Z-reference-v2.md) |
| Vérifications d'isolement (facultatives) | environ 0,03 <span>$</span> chacune | [journal](journal.md), J7.1 |
| **Total** | **environ 0,10 <span>$</span>**, le double avec une répétition | |

Le rapport sans IA, la remise à zéro, le trafic et les pannes ne coûtent rien.
