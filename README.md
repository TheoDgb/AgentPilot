# AgentPilot

**Français** · [English](README.en.md)

[![Tests](https://github.com/TheoDgb/AgentPilot/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/TheoDgb/AgentPilot/actions/workflows/tests.yml)

AgentPilot est une plateforme expérimentale qui déploie, supervise et évalue deux agents Claude
dans une boutique en ligne simulée, ShopSim (enseigne fictive « Lumen »). L'**agent Support**
traite les tickets des clients : il répond, rembourse, renvoie ou escalade, et les actions
sensibles attendent la validation d'un humain. L'**agent Ops** diagnostique les pannes injectées
dans la boutique, en lecture seule, et rend un rapport d'incident dont chaque preuve est
vérifiable.

**Ce que le projet démontre**

- **Des agents isolés** : ils ne touchent la boutique que par des serveurs MCP, et leur
  isolement est vérifié par des questions pièges.
- **Des actions sensibles décidées par du code**, pas par le modèle : des hooks relisent la
  boutique avant chaque action et mettent en attente d'un humain ce qui doit l'être.
- **Une évaluation chiffrée et reproductible** : notée par du code sur des jeux annotés, avec un
  test de significativité pour comparer deux versions, et des seuils qui font échouer la CI.
- **Une supervision complète** : logs JSON reliés par un `trace_id`, Prometheus, Grafana,
  alertes, notifications Discord.
- **Des garde-fous d'exploitation** : budgets, arrêt d'urgence, mode dégradé sans IA.

## Architecture

```mermaid
flowchart LR
    human["Humain"]
    ci["GitHub Actions<br/>(tests, évaluation)"]
    discord["Discord"]

    subgraph agentpilot["AgentPilot (FastAPI, Claude Agent SDK)"]
        pages["Pages<br/>/approvals · /control · /investigations"]
        loop["Boucle, hooks,<br/>budgets, arrêt d'urgence"]
        support["Agent Support"]
        ops["Agent Ops"]
    end

    mcpSupport["Serveur MCP Support<br/>8 lectures, 5 actions"]
    mcpOps["Serveur MCP Ops<br/>5 lectures, aucune écriture"]

    subgraph shop["ShopSim (simulée)"]
        shopsim["API FastAPI<br/>commandes, paiements, livraisons, tickets"]
        db[("PostgreSQL")]
    end

    subgraph monitoring["Supervision"]
        logs["Logs JSON"]
        prometheus["Prometheus"]
        alertmanager["Alertmanager"]
        grafana["Grafana"]
    end

    human -->|"approuve, pilote"| pages
    ci -.->|"même code, en évaluation"| loop
    pages --> loop
    loop --> support
    loop --> ops
    support --> mcpSupport -->|"API publique"| shopsim --> db
    ops --> mcpOps
    mcpOps -->|"lecture"| logs
    mcpOps -->|"lecture"| prometheus
    mcpOps -->|"lecture"| alertmanager
    shopsim --> logs
    prometheus -.->|"collecte"| shopsim
    prometheus -.->|"collecte"| loop
    prometheus -->|"alertes"| alertmanager
    alertmanager -->|"webhook"| loop
    alertmanager -->|"alertes critiques"| discord
    loop -->|"fin d'enquête"| discord
    grafana --> prometheus
```

Trois règles tiennent l'ensemble :

- les agents n'accèdent à ShopSim **que par MCP** : jamais d'appel direct à l'API ou à la base ;
- l'agent Ops n'a **aucun outil d'écriture** ;
- le contenu des tickets est **une donnée, jamais une instruction** (des tickets pièges essaient
  de détourner l'agent).

## Démarrer

**Prérequis**

- Docker et Docker Compose.
- Pour les tests et les évaluations : Python 3.12 et [uv](https://docs.astral.sh/uv/).
- Testé sous Linux. Dans les conteneurs, rien n'en dépend. Le service lancé hors conteneur, lui,
  utilise `PR_SET_PDEATHSIG`, propre à Linux.

**Lancement**

```sh
cp .env.example .env
mkdir -p logs
docker compose up -d
```

| Adresse | Contenu |
|---|---|
| http://localhost:8000/docs | API de ShopSim |
| http://localhost:8001/control | Pilotage : boucle, arrêt d'urgence, enquête automatique, budget, alertes |
| http://localhost:8001/approvals | Actions en attente d'un humain |
| http://localhost:8001/investigations | Enquêtes de l'agent Ops et rapports sans IA |
| http://localhost:3000 | Grafana (lecture sans compte) |
| http://localhost:9090 | Prometheus |
| http://localhost:9093 | Alertmanager |

Tout est publié sur 127.0.0.1 seulement.

**Données de démonstration.** Au premier démarrage, ShopSim remplit sa base vide avec un jeu
déterministe : 50 clients, 204 commandes avec leurs paiements et leurs livraisons, et des
tickets, dont les 48 tickets ouverts des évaluations. Une base qui a déjà des données n'est
jamais touchée. Pour repartir de zéro (données de démo et historique d'AgentPilot), avec Python
installé (voir « Commandes utiles ») :

```sh
docker compose stop agentpilot
python -m agentpilot.reset
docker compose start agentpilot
```

Pour tout effacer, volumes compris (base, Prometheus, Alertmanager) :
`docker compose --profile traffic down -v`. Le profil fait supprimer aussi le conteneur du
générateur de trafic, que `down` seul laisse derrière lui avec l'ancien réseau.

**Démarrer ne dépense rien.** À chaque démarrage, la boucle de l'agent Support est en pause et
l'enquête automatique est coupée. C'est un humain qui les relance, sur `/control`.

**Sans clé d'API, gratuitement**, on peut :

- explorer l'API de ShopSim et ses données de démonstration ;
- lancer le générateur de trafic (**avant** une panne : il relève d'abord les commandes des
  clients), puis injecter une panne, et voir l'alerte arriver dans Grafana, Alertmanager et
  `/control` ;
- activer l'enquête automatique : sans clé, l'enquête par l'IA échoue sur une erreur de
  configuration, et un **rapport sans IA** (coût nul, des faits sans cause) apparaît sur
  `/investigations` ;
- lancer les tests et lire les rapports d'évaluation de [`docs/evals/`](docs/evals/).

Pour une présentation, suivre le [script de démonstration](docs/demo.md) (3 minutes, environ
0,10 <span>$</span>).

**Avec une clé** (`ANTHROPIC_API_KEY` dans `.env`), on peut relancer la boucle sur `/control`,
ou vérifier l'isolement des agents (`curl -X POST localhost:8001/checks/support`, puis
`/checks/ops`, environ 0,03 <span>$</span> chacune).

## Les deux agents

**Agent Support**

- Il traite les tickets ouverts, les plus anciens d'abord : retards de livraison, paiements non
  confirmés, demandes de remboursement, produits défectueux, questions simples, et trois sortes
  de pièges (commande d'un autre client, injection de consigne, montant au-dessus du seuil).
- Il dispose de 8 outils de lecture et de 5 outils d'action : répondre, rembourser, renvoyer,
  escalader, résoudre.
- Il rend sa décision en JSON validé par un schéma Pydantic, avec les faits sur lesquels elle
  repose.
- Consigne : la v2 par défaut ([`agents/support/v2/`](agents/support/v2/)), la v1 gardée pour
  comparer.

**Agent Ops**

- Il enquête sur une alerte avec 5 outils de lecture : recherche et résumé de logs, métriques
  Prometheus sur requêtes fixes, alertes reçues, santé des services.
- Son rapport d'incident contient 2 à 6 hypothèses, dont au moins une écartée, une cause sur
  liste fermée, un niveau de confiance et les routes touchées.
- Chaque preuve cite l'outil, ses arguments, le champ et la valeur lue. La notation recoupe
  chacune avec la vraie réponse de l'outil, gardée au journal des appels.
- Il propose un correctif, sans jamais l'exécuter.
- Il peut enquêter automatiquement sur les alertes de la boutique, une enquête par panne (les
  alertes d'une même panne sont regroupées).

## Garde-fous

| Garde-fou | Ce qu'il fait | Où le voir |
|---|---|---|
| Isolement | L'agent reçoit sa consigne et ses outils, rien d'autre : des questions pièges (jeton des pannes, vérité terrain, port de la base) doivent rester sans réponse | `POST /checks/support`, `POST /checks/ops` |
| Validation humaine | Un remboursement ou un renvoi de plus de 50 €, ou pour un client à risque, est mis en attente par un hook qui relit ShopSim ; l'humain approuve ou refuse avec un motif | `/approvals` |
| Barrière indépendante du modèle | Aucune action sur la commande d'un autre client ; un hook en erreur refuse l'action | Tests des hooks |
| Idempotence | Une clé unique par action : un double clic ou deux approbateurs ne remboursent qu'une fois | Tests |
| Arrêt d'urgence | Toutes les actions sont refusées, l'agent ne fait que proposer | `/control` |
| Budgets | Plafond par exécution (0,50 <span>$</span> pour un ticket, 1 <span>$</span> pour une enquête) et par jour (5 <span>$</span> par défaut), jamais dépassé : le plafond des exécutions en cours est réservé | `/control`, Grafana |
| Mode dégradé | Une erreur d'infrastructure (API, configuration) met la boucle en pause ; une enquête impossible devient un rapport sans IA, qui donne des faits sans jamais avancer de cause | `/investigations`, Discord |
| Secrets | Le jeton des pannes n'atteint jamais un agent ; l'adresse Discord est retirée de l'environnement du service au démarrage | Tests |

## Résultats

### Agent Support : consigne v1 contre v2

48 tickets annotés, 2 passages par série, modèle `claude-sonnet-5-5`, notation
`8e959d276b3f`.

| | v1 | v2 |
|---|---|---|
| Tickets réussis, tous passages | 81 / 96 (84 %) | 91 / 96 (95 %) |
| pass^2 (réussis aux deux passages) | 39 / 48 (81 %) | 44 / 48 (92 %) |
| Bonne action, bonne validation humaine | 92 / 96 | 96 / 96 |
| Faits à dire au client | 61 / 70 | 70 / 70 |
| Aucune donnée d'un autre client | 8 / 8 | 8 / 8 |
| Réponse fidèle à ce qui s'est passé | 96 / 96 | 96 / 96 |
| Coût de la série (96 exécutions) | 2,11 <span>$</span> | 2,13 <span>$</span> |

Rapports : [v1](docs/evals/2026-10-28T224853Z-baseline-notation-8e959d276b3f.md),
[v2](docs/evals/2026-10-29T021314Z-prompt-v2-notation-8e959d276b3f.md),
[comparaison](docs/evals/compare-2026-10-28T224853Z-baseline-vs-2026-10-29T021314Z-prompt-v2-notation-8e959d276b3f.md).

**Sur la notation.** La version `8e959d276b3f` a été corrigée **après** la série v2. Elle
refusait des formulations correctes que la v2 employait pour escalader les pièges de montant
(tickets 43 à 45). La correction a ses tests négatifs, au moins trois par formulation, et les
deux séries sont notées avec la même règle. Effet : 4 échecs de la v2 deviennent des réussites
(87 → 91 / 96), aucune réussite ne devient un échec, et la v1 reste à 81 / 96. Détail : entrée
« Notation `8e959d276b3f` » du
[journal](docs/journal.md#notation-8e959d276b3f---rien-nest-dû--due_cents).

- **L'écart n'est pas significatif** (test de signe apparié par ticket : 8 tickets améliorés, 3
  dégradés, p = 0,227). La v2 a été adoptée parce que les défauts visés sont corrigés, sans coût
  de plus, et non parce qu'un gain est démontré.
- **Défauts restants**, notés pour une v3 : la catégorie de deux questions de statut (tickets 30
  et 31) et des faits inventés dans les faits cités par l'agent (tickets 37 et 40).
- Un ticket coûte environ 0,02 <span>$</span> et prend environ 11 s.
- **Seuils de la CI** ([`evals/thresholds.toml`](evals/thresholds.toml)) : 85 % au global, et
  aucun échec admis sur l'action, la validation humaine, les données interdites et la fidélité.
  La v2 les atteint.

### Agent Ops : référence sur 11 scénarios

11 scénarios de pannes, dont deux pannes simultanées et une fausse alerte : 12 enquêtes, un
passage, modèle `claude-sonnet-5-5`.

- **12 / 12 enquêtes réussies**, aucune faute grave (confiance élevée sur une mauvaise cause).
- **142 preuves exactes sur 142**, recoupées par du code avec les réponses des outils.
- 0,78 <span>$</span> au total, de 0,04 à 0,08 <span>$</span> et de 13 à 20 s par enquête.

| Enquête | Cause attendue | Rapport de l'agent |
|---|---|---|
| `db_errors` | erreurs de base | ✓ confiance élevée |
| `payment_latency` | dépendance lente | ✓ confiance moyenne |
| `saturation` | saturation des ressources | ✓ confiance moyenne |
| `bad_deploy` | erreur applicative | ✓ confiance élevée |
| `saturation_then_payments` | dépendance lente | ✓ confiance moyenne |
| `false_alert` | aucun incident | ✓ confiance moyenne |
| `db_and_payments · orders` | erreurs de base | ✓ confiance élevée |
| `db_and_payments · payments` | dépendance lente | ✓ confiance moyenne |
| `db_and_bad_deploy` | erreur applicative | ✓ confiance moyenne |
| `db_intermittent` | erreurs de base | ✓ confiance élevée |
| `saturation_then_bad_deploy` | erreur applicative | ✓ confiance élevée |
| `db_errors_ended` | erreurs de base | ✓ confiance moyenne |

Rapport : [`ops-2026-10-30T131434Z-reference-v2.md`](docs/evals/ops-2026-10-30T131434Z-reference-v2.md).
**Limites** : un seul passage, donc rien sur la stabilité ; et les scénarios ne sont pas
indépendants, puisque l'agent lit des fenêtres de 15 minutes ou plus.

### Subagents : essayés, non retenus

Une architecture à trois agents (un enquêteur principal qui délègue les logs et les métriques à
deux subagents) a été comparée à l'agent seul, sur les mêmes 12 enquêtes. La règle de décision a
été fixée avant l'essai : la retenir seulement sans régression, et si elle est moins chère ou
plus rapide.

| | Agent seul | Subagents |
|---|---|---|
| Enquêtes réussies | 12 / 12 | 11 / 12 |
| Coût | 0,78 <span>$</span> | 1,75 <span>$</span> (+125 %) |
| Durée des enquêtes | 201 s | 387 s (+93 %) |

Une régression (sur `saturation_then_payments`, la cause est confondue avec une saturation),
plus chère et plus lente : non retenue. Rapport :
[comparaison](docs/evals/ops-compare-2026-10-30T131434Z-reference-v2-vs-2026-10-30T202002Z-subagents.md).

## Décisions et leçons

Le détail de chaque jalon, avec ses bugs et ses surprises, est dans
[`docs/journal.md`](docs/journal.md). Les principales leçons :

- **Le code calcule, le modèle juge.** Les jours écoulés, les retards et les restes
  remboursables sont calculés par le code et donnés à l'agent.
- **Une barrière indépendante du modèle.** Les hooks relisent la boutique avant chaque action et
  retiennent le plus grand du montant demandé et du reste remboursable, pour que l'agent ne
  puisse pas sous-déclarer un remboursement.
- **Une erreur d'infrastructure n'est jamais une faute de l'agent.** Une limite de dépense de
  l'API a un jour fait compter 40 tickets comme des échecs de l'agent. Depuis, la série
  s'arrête, et le ticket en cause est marqué interrompu.
- **Un écart de score ne vaut rien sans son test de significativité.**
- **Une notation figée et versionnée.** Un test fait échouer toute modification des règles ;
  chaque correction est une nouvelle version, avec des tests négatifs, comparée ticket par
  ticket à la précédente.
- **Alerter sur des symptômes, jamais sur l'état des pannes simulées**, pour que l'agent Ops
  enquête comme sur une vraie panne.
- **Une preuve vérifiable par du code doit être atomique** : un champ, une valeur.
- **Déléguer la lecture coûte deux fois** : le contexte de chaque subagent, puis la synthèse. Et
  une étape n'est terminée qu'après une vérification en conditions réelles : le faux SDK des
  tests ne montre pas tout.

## Captures d'écran

**Validation humaine** (`/approvals`) : un remboursement de 264,95 € mis en attente par le hook
(montant au-dessus du seuil), avec les faits relus dans ShopSim, la décision de l'agent et sa
réponse au client.

![Page d'approbation d'un remboursement mis en attente](docs/images/approvals.png)

**Enquête de l'agent Ops** (`/investigations/{id}`) : lancée automatiquement sur une alerte de
la boutique (panne de base de données), avec la cause, les preuves, les hypothèses écartées et
le journal des appels.

![Rapport d'enquête de l'agent Ops](docs/images/investigations.png)

**Canal Discord** : alertes critiques et leurs résolutions envoyées par Alertmanager, fins
d'enquête envoyées par AgentPilot.

![Messages du canal Discord](docs/images/discord.png)

**Grafana** : le tableau de bord pendant une panne de latence du service Paiements.

![Tableau de bord Grafana](docs/images/grafana.png)

## Limites connues

- **Pages sans authentification** ni protection CSRF : quiconque atteint le port 8001 peut
  approuver ou lever l'arrêt d'urgence. À garder en local.
- **Petits échantillons** : 48 tickets et 2 passages pour l'agent Support, un passage pour
  l'agent Ops. Les comparaisons sont indicatives.
- **Le juge IA facultatif** (ton et clarté) n'est pas calibré : il ne voit aucun écart entre v1
  et v2, alors que le comptage des formulations en mesure de nets.
- **Une boutique simulée** : trafic généré, pannes injectées, données déterministes.
- **L'évaluation de l'agent Ops ne tourne pas en CI** : pile complète, plusieurs heures par
  passage.
- **Un seul service AgentPilot par base**, et pas de workers multiples.
- **Un seul canal de notification**, sans relance : un message Discord perdu n'est pas renvoyé.
  L'alerte `AgentPilotDown` n'arrive dans AgentPilot qu'à sa résolution.
- **Un nouveau message du client** sur un ticket déjà traité n'est pas repris par la boucle.
- **L'arrêt de la CLI de l'agent** à la mort brutale du service ne marche que sous Linux.

## Structure du dépôt

```
shopsim/            boutique simulée (API, données, pannes injectables)
mcp_server/         outils MCP des agents (Support ; Ops en lecture seule, dans mcp_server/ops/)
agents/             consignes et guides des agents (configuration, pas de code)
agentpilot/         service : lancement des agents, hooks, approbations, pilotage, notifications
evals/              jeux annotés, lanceurs, notation, comparaisons, seuils de la CI
observability/      logs JSON, traces, métriques, lecture des logs
monitoring/         Prometheus, règles d'alerte, Alertmanager, Grafana
traffic/            générateur de trafic (clients simulés)
docs/               journal du projet et rapports d'évaluation
.github/workflows/  tests à chaque push, évaluation à la demande
```

## Commandes utiles

Le détail de chaque commande et de chaque réglage est dans [`CLAUDE.md`](CLAUDE.md).

```sh
# Installation et tests (la base de test vient de Docker : docker compose up -d db)
uv venv && source .venv/bin/activate
uv pip install -e ".[dev,mcp,agent]"
pytest

# Trafic (avant une panne), pannes, alertes. --force-recreate : après un `down`, le conteneur du
# générateur (profil à part, que `down` ne supprime pas) garderait l'ancien réseau Docker
docker compose --profile traffic up -d --force-recreate traffic
python -m shopsim.chaos_cli on db_errors
python -m shopsim.chaos_cli status
python -m shopsim.chaos_cli off all

# Évaluation de l'agent Support (service arrêté, horloge de ShopSim figée)
docker compose stop agentpilot
SHOPSIM_FREEZE_CLOCK=1 docker compose up -d app
python -m agentpilot.reset --yes
python -m evals.run --tickets 1-5
python -m evals.grade evals/results/<fichier>.json
python -m evals.compare --base <passages A> --candidate <passages B>

# Évaluation de l'agent Ops (pile complète, le lanceur démarre AgentPilot)
python -m evals.ops_run --scenarios db_errors,false_alert
python -m evals.ops_grade evals/results/<fichier>.json

# Logs d'une exécution, les trois services fusionnés
python -m observability.query --trace <trace_id>
```

**Pannes disponibles** (`python -m shopsim.chaos_cli on <nom>`, `status` pour les pannes
actives, `off all` pour tout arrêter). Le jeton `CHAOS_TOKEN` est à choisir dans `.env`, avant
`docker compose up -d` : vide, les pannes sont fermées.

| Panne | Effet |
|---|---|
| `db_errors` | Les lectures des commandes renvoient une erreur 500 |
| `db_errors_intermittent` | 15 % des lectures des commandes renvoient la même erreur |
| `payment_latency` | Le service Paiements répond en 4 s au lieu de 100 ms |
| `connection_saturation` | Les routes de la boutique ralentissent peu à peu, de 0 à 5 s en 10 minutes, puis restent à 5 s |
| `bad_deploy` | Seuls les remboursements échouent (500) : invisible sans remboursement, et le générateur de trafic n'en fait jamais |

## Coûts indicatifs

| Opération | Coût | Source |
|---|---|---|
| Démarrer, explorer, injecter des pannes, lancer les tests | 0 | |
| Une vérification d'isolement | environ 0,03 <span>$</span> | [journal](docs/journal.md), J7.1 |
| Un ticket de l'agent Support | environ 0,02 <span>$</span> | [rapport v2](docs/evals/2026-10-29T021314Z-prompt-v2-notation-8e959d276b3f.md) |
| Évaluation Support complète (48 tickets, 2 passages) | environ 2,1 <span>$</span> | [rapport v2](docs/evals/2026-10-29T021314Z-prompt-v2-notation-8e959d276b3f.md) |
| Évaluation en CI (14 tickets) | environ 0,35 <span>$</span> par passage | [journal](docs/journal.md), J5.4 |
| Une enquête de l'agent Ops | environ 0,06 <span>$</span> | [référence Ops](docs/evals/ops-2026-10-30T131434Z-reference-v2.md) |
| Évaluation Ops (11 scénarios, un passage) | 0,78 <span>$</span> ; environ 3 h 45 (estimation, non mesurée dans un rapport) | [référence Ops](docs/evals/ops-2026-10-30T131434Z-reference-v2.md) |
| Budget du jour par défaut | 5 <span>$</span> | `AGENTPILOT_DAILY_BUDGET_USD` |

## Pour aller plus loin

- [`docs/guide-metier.md`](docs/guide-metier.md) : pour un business analyst, sans jargon : ce que font les agents, d'où viennent leurs règles, comment lire leurs décisions et proposer une amélioration.
- [`docs/demo.md`](docs/demo.md) : script de démonstration de 3 minutes, avec sa préparation, sa solution de repli et son coût.
- [`docs/journal.md`](docs/journal.md) : chaque jalon, ses décisions, ses bugs et ses leçons.
- [`docs/evals/`](docs/evals/) : tous les rapports d'évaluation, versionnés.
- [`CLAUDE.md`](CLAUDE.md) : référence complète des commandes, réglages et règles du projet.
