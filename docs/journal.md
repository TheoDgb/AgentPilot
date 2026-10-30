# Journal du projet

Ce qui a été fait à chaque jalon, les bugs ou surprises rencontrés, et les leçons retenues.
Reconstitué le 2026-10-29 à partir de la section Avancement de `CLAUDE.md` (version actuelle
et versions d'avant les condensations `d2bc3d4` et `b59000c`) et des messages de commit. Rien
n'y est ajouté de mémoire : là où les sources ne disent rien, le journal le dit. Le résumé de
l'avancement et les règles en vigueur restent dans `CLAUDE.md`.

## J0 — Dépôt et outillage

*2026-10-23 (premiers commits, `.gitignore`) et 2026-10-24 (`2281e9c`)*

**Fait**
- Dépôt, Python 3.12 géré par uv, `pyproject.toml`, commande `/test`.
- Arborescence posée d'avance avec des fichiers vides (`agents/support/`, `evals/`,
  `mcp_server/`, `shopsim/`…), dont certains ont été supprimés plus tard sans avoir servi :
  `agentpilot/approvals.py` et `agentpilot/runner.py` en J3.5, `evals/run_eval.py` en J5.1.

**Bugs ou surprises** : aucun noté dans les sources.

**Leçons** : aucune notée dans les sources.

## J1 — ShopSim

*2026-10-24 au 2026-10-25 (`208f1aa` à `528f478`)*

**Fait**
- J1.1 (`208f1aa`) : Docker Compose (PostgreSQL et FastAPI), clients et commandes,
  `GET /orders/{id}`, seed, premier test ; base publiée sur le port 5433.
- J1.2 (`5d8e475`) : service Paiements, remboursements idempotents, `/health`.
- J1.3 (`02b0ca0`) : service Livraisons, date de référence, dates cohérentes avec les statuts.
- J1.4a (`f6b961e`) : clients et tickets en lecture.
  - `GET /customers/{id}` (ancienneté, tickets résolus, remboursements) et
    `GET /customers/{id}/orders`.
  - `GET /tickets` paginé (filtre `status`) et `GET /tickets/{id}` avec ses messages.
  - Seed : 48 tickets ouverts cohérents avec les données (8 catégories, dont 3 types de
    pièges) et 12 tickets historiques. Clients à risque créés par des gestes commerciaux
    partiels, et 4 petites commandes livrées (ids 201 à 204) pour les actions automatiques.
  - Vérité terrain écrite seulement dans `evals/tickets.jsonl`, règles métier dans
    `evals/policy.py`.
  - Tests : aucune fuite de la vérité terrain dans l'API, déterminisme du seed.
- J1.4b (`1f5f48b`, planifié à part dans `49053da`) : actions sur les tickets.
  - Réponse avec `Idempotency-Key` obligatoire : 400 sans clé, 200 au rejeu, 409 si la clé
    sert à une autre demande.
  - Escalade avec motif : même motif rejoué, 200 sans effet ; autre motif, 409.
  - Résolution : rejouée, 200 sans effet.
  - Répondre ou escalader un ticket résolu : 409.
- J1.5 (`528f478`) : endpoint Chaos.
  - 4 pannes, avec leur état en base (partagé entre processus, remis à zéro par le seed).
  - Jeton `X-Chaos-Token` comparé à `CHAOS_TOKEN` (vide = Chaos fermé), jamais donné aux
    agents.
  - Les logs imitent de vraies erreurs, sans mention de la panne ; `/health` et `/chaos` ne
    sont jamais touchés.
- **Décision** : ShopSim terminé, ses API ne changent plus sans accord. Ajouts validés depuis :
  - `GET /customers/{id}/refunds` (J2.1) ;
  - `POST /orders/{id}/resend` (J3.1) ;
  - `GET /clock` et `SHOPSIM_FREEZE_CLOCK` (J3.2) ;
  - `author` sur `POST /tickets/{id}/reply` (J3.4) ;
  - `GET /metrics` (J4.2) ;
  - `POST /tickets`, jamais exposé aux agents (J4.3).

**Bugs ou surprises** : aucun noté dans les sources (seulement une coquille de CLAUDE.md
corrigée, `b9ad12e`).

**Leçons** : aucune notée comme telle. Principe posé dès J1.4b : un rejeu d'une action déjà
faite réussit sans effet (200), 409 seulement si la demande est incompatible.

## J2 — Serveur MCP et agent Support en headless

*2026-10-26 (`42867a8`, `c275c9e`)*

**Fait**
- J2.1 : serveur MCP en lecture seule (SDK officiel), qui appelle l'API HTTP de ShopSim et
  jamais la base.
  - 8 outils : tickets ouverts, ticket, commande, client, ses commandes, ses remboursements,
    paiement, livraison.
  - Erreurs de ShopSim (404, 5xx, délai dépassé, service injoignable) renvoyées à l'agent en
    message lisible, sans planter le serveur.
  - Aucun outil d'action, aucun accès au Chaos ; `.mcp.json` à la racine.
  - Dépendances dans l'extra `[mcp]`, absent de l'image ShopSim.
  - Ajout validé à ShopSim : `GET /customers/{id}/refunds`, pour la règle « client à risque ».
- J2.2 : agent Support en headless (`agentpilot/run_support.py`), décision JSON validée
  (`SupportDecision`), sans action.
  - Consigne et skill `refund-policy` reprise de `evals/policy.py`.
  - Permissions limitées aux outils `mcp__shopsim__*` : ni fichiers, ni shell, ni web.
  - `--check` : l'agent restitue sa politique et sa signature, et ne connaît rien du CLAUDE.md
    développeur (questions pièges). Isolement ainsi prouvé.

**Bugs ou surprises** : aucun noté dans les sources de J2 (voir J3.1 pour la date réelle
retenue par l'agent lors d'un `--check`).

**Leçons** : aucune notée comme telle.

## J3 — Agent Support dans le service AgentPilot

*2026-10-26 au 2026-10-27 (`182df9b` à `1b8dfd5`)*

**Fait**
- J3.1 (`182df9b`) : 5 outils d'action dans le serveur MCP (répondre, rembourser, renvoyer,
  escalader, résoudre).
  - Chargés seulement si `SHOPSIM_MCP_ACTIONS=1` ; lecture seule par défaut.
  - Clés d'idempotence calculées par le serveur : un rejeu renvoie `already_done`.
  - Ajouts validés à ShopSim : `POST /orders/{id}/resend` et une horloge de simulation unique
    (`clock.now`), avec un test qui interdit `datetime.now()` ailleurs.
- J3.2 (`226dfe4`) : agent lancé par le Claude Agent SDK dans le service FastAPI ; exécutions
  enregistrées dans le schéma `agentpilot`.
  - Isolement par le SDK : aucune source de réglages, consigne de l'agent seule, 8 outils de
    lecture, dossiers de travail et de configuration temporaires vides.
  - Bloc `computed` dans les outils de lecture : jours depuis la livraison, retard, reste
    remboursable, remboursements dans la fenêtre de risque.
  - Ajouts validés à ShopSim : `GET /clock` et `SHOPSIM_FREEZE_CLOCK=1`.
- J3.3 (`8ff81a7`) : actions sous hooks.
  - Le code relit ShopSim lui-même et met en attente tout remboursement ou renvoi de plus de
    50 €, ou pour un client à risque.
  - Réponses et escalades sans validation.
  - Refus durs : autre ticket, commande d'un autre client, ShopSim illisible.
  - Journal de chaque appel dans `agentpilot.tool_calls`. Sans hooks, le lancement est refusé.
- J3.4 (`bc3c313`) : page d'approbation `/approvals`, rendue côté serveur (Jinja2).
  - Approuver exécute l'action une seule fois, même après un double clic ou avec deux
    approbateurs (`UPDATE … WHERE status = 'pending'`), après revérification dans ShopSim et
    avec la clé enregistrée.
  - Refuser exige un motif.
  - Ajout validé à ShopSim : `author` sur les réponses, jamais choisi par l'agent.
  - Nouvelle dépendance : jinja2.
- J3.5 (`1b8dfd5`) : traitement continu.
  - Boucle de 2 exécutions au plus, jamais deux fois le même ticket en parallèle (index unique
    partiel, 409).
  - Reprise au démarrage sans double action.
  - Arrêt d'urgence relu par les hooks avant chaque action.
  - Budget du jour avec pause automatique.
  - Attente croissante si ShopSim est en panne ; page `/control`.
  - Pas d'agent orphelin : `claude_guard.py` (`PR_SET_PDEATHSIG`) et `parent_watch.py`.
- **Vérifié en conditions réelles** : approbation et refus. Ctrl+C et `kill -9` pendant des
  exécutions : relance unique, aucune double réponse ni double remboursement, aucun processus
  orphelin. Arrêt d'urgence et panne de ShopSim.

**Bugs ou surprises**
- Lors d'un `--check`, l'agent avait retenu la date réelle que Claude Code ajoute à la
  consigne. La consigne précise désormais que seule la date de la simulation compte (J3.1).
- Premier lancement réel de la boucle : `DetachedInstanceError` dans `new_run`. Les tests
  utilisaient une connexion partagée avec points de sauvegarde (`expire_on_commit=False`,
  J3.2), ce qui masquait la lecture d'un objet hors de sa session. Corrigé, avec un test de
  régression ; les tests passent depuis par une seule fabrique de sessions, avec de vraies
  connexions et de vrais commits (J3.5).
- Limites laissées en place, notées dans CLAUDE.md :
  - pages d'approbation et de pilotage sans authentification ni protection CSRF ;
  - exécution interrompue après une escalade, sans décision enregistrée ;
  - arrêt de la CLI à la mort du service sous Linux seulement.

**Leçons** : aucune notée comme telle. Principes posés dans les commits :
- « Le code calcule, le modèle juge » : les jours, retards et restes remboursables sont
  calculés par le code et donnés à l'agent (J3.2).
- Barrière indépendante du modèle : pas de remboursement ni de renvoi d'une commande qui
  n'appartient pas au client du ticket (J3.1). La valeur d'un remboursement retenue par le hook
  est le plus grand du montant demandé et du reste remboursable, pour que l'agent ne puisse pas
  sous-déclarer (J3.3).
- Un hook en erreur refuse l'action (J3.5).
- Tester les sessions comme en production (vraies connexions, vrais commits), sinon un bug
  n'apparaît qu'au premier lancement réel (J3.5).

## J4 — Logs, métriques, trafic et alertes

*2026-10-27 au 2026-10-28 (`f7396ea` à `7665b28`)*

**Fait**
- J4.1 (`f7396ea`) : logs JSON avec `trace_id` de bout en bout.
  - Paquet `observability/` : une ligne par événement, champs sur liste blanche, filet
    anti-secret.
  - Trace W3C : exécution → serveur MCP (`TRACEPARENT`) → ShopSim (`traceparent`).
  - Requêtes `/chaos` seulement dans `chaos.jsonl` ; lecture par
    `python -m observability.query`, même code que le futur outil de l'agent Ops.
- J4.2 (`f81f670`) : métriques Prometheus (ShopSim et AgentPilot) et tableau de bord Grafana
  provisionné.
  - Aucune métrique sur l'état des pannes ; aucun identifiant ni montant en étiquette.
  - Nouvelle dépendance : prometheus-client.
  - Aussi : `python -m shopsim.chaos_cli` et `python -m agentpilot.reset` (verrou du service).
- J4.3 (`11c16f4`) : générateur de trafic (`python -m traffic`).
  - Liste blanche de routes, débit en boucle ouverte.
  - Nouveaux tickets désactivés par défaut et plafonnés.
  - Ajout validé à ShopSim : `POST /tickets`.
- J4.4 (`7665b28`) : 8 alertes Prometheus sur symptômes seulement, testées par `promtool`.
  Alertmanager les envoie à AgentPilot (`agentpilot.alerts`), sans action automatique.
- **Vérifié en conditions réelles**
  - J4.1 : même trace dans les trois services sur un vrai run ; panne `db_errors` visible sans
    marqueur ; requêtes Chaos seulement dans `chaos.jsonl` (ni dans les autres fichiers, ni dans
    `docker compose logs`).
  - J4.2 :
    - cibles collectées ;
    - latence et 5xx des pannes visibles, sans trace de Chaos dans `/metrics` ;
    - remboursement du ticket 19 mis en attente puis approuvé, visible sur Grafana ;
    - remise à zéro refusée service lancé.
  - J4.3 :
    - 204 commandes découvertes dans Docker ;
    - trafic de fond pendant `payment_latency` et `db_errors` ;
    - arrêt propre par SIGTERM ;
    - 2 × 2 tickets créés et relus, cohérents avec les données, `--max-tickets` respecté.
  - J4.4 :
    - 10 min de trafic normal sans alerte ;
    - `db_errors` : `ShopSimHighErrorRate` sur `/orders/{order_id}` et
      `/customers/{customer_id}/orders`, résolue environ 2 min après l'arrêt ;
    - `payment_latency` : `ShopSimHighLatency` sur `/payments/{order_id}`, résolue environ
      5 min après, le p95 étant calculé sur 5 min ;
    - une ligne par occurrence malgré les envois répétés, aucune exécution créée.

    Non essayée en réel : `AgentPilotDown`, couverte par les tests.

**Bugs ou surprises**
- Le SDK MCP posait son propre gestionnaire de logs texte : retiré (J4.1).
- Le chemin `evals/tickets.jsonl` a dû être rétabli dans la ligne J1 de CLAUDE.md : une
  question piège de `--check` en dépend (J4.1).
- Reprise : la consigne listait les actions de toutes les exécutions passées, si bien qu'une
  relance après un seed croyait le ticket déjà escaladé. Elle ne liste plus que les actions des
  exécutions interrompues ou en échec depuis la dernière réussie (J4.2).
- Le test du tableau de bord déduisait les codes HTTP des requêtes déjà faites : il dépendait
  de l'ordre d'exécution des tests. Corrigé (J4.4).
- Limites laissées en place, notées dans CLAUDE.md :
  - supervision Linux seulement (réseau hôte) ;
  - compteurs remis à zéro à chaque redémarrage ;
  - logs sans rotation (environ 100 Mo par jour avec le générateur à 5 req/s) ;
  - `logs/` à créer avant `docker compose up` ;
  - webhook d'alertes sans authentification ;
  - `AgentPilotDown` enregistrée seulement à sa résolution.

**Leçons** : aucune notée comme telle. Principe posé dans les commits : alerter sur des
symptômes, jamais sur l'état du Chaos, pour que l'agent Ops (J6) diagnostique comme sur une
vraie panne (J4.4).

## J5 — Évaluation en CI

*2026-10-28 au 2026-10-29 (`42c3681` à `1d50a1d`)*

### J5.1 : lanceur d'évaluation (`42c3681`)

**Fait**
- `python -m evals.run` : l'agent Support est lancé sur les tickets de la vérité terrain, avec
  le même code que le service (`Worker`, hooks, outils, isolement, 2 exécutions au plus),
  précédé de la vérification d'isolement. Nouveau déclencheur `eval`.
- Refus avant tout lancement :
  - `CHAOS_TOKEN` dans le shell ;
  - service lancé (verrou de la base) ;
  - AgentPilot ou ShopSim non remis à zéro ;
  - horloge non figée ;
  - panne Chaos active ou invérifiable.
- Actions sensibles mises en attente, jamais approuvées. Plafond jamais dépassé (0,50 $
  réservés par exécution en cours).
- Résultats datés dans `evals/results/` : décision, actions faites, en attente ou refusées,
  réponses, statut du ticket relu, coût, durée, erreurs, `trace_id`. États d'arrêt :
  `budget_exhausted`, `interrupted`, `aborted`.
- **Vérifié en conditions réelles** sur 5 tickets (1, 16, 19, 21, 37) pour 0,19 $ :
  - refus attendus tant que la panne `db_errors` était active ;
  - réponse ;
  - remboursement de 10,45 € fait ;
  - remboursement de 385,09 € en attente et absent de ShopSim ;
  - refus hors délai ;
  - injection escaladée.

**Bugs ou surprises**
- La vérification de remise à zéro refusait les 12 tickets historiques du seed (n° 49 à 60,
  déjà traités), absents de la ShopSim de test : corrigé.
- Les exécutions d'un test fuyaient sur le suivant : la fixture `ready` vide maintenant les
  tables d'AgentPilot.

**Leçons**
- Une vérification de l'état de départ s'essaie aussi sur le vrai seed : la ShopSim de test
  n'a pas les tickets historiques.

### J5.2 : notation et rapport (`75bfb95`)

**Fait**
- `python -m evals.grade` : 8 critères vérifiés par du code, sans IA.
  - `category`, `action`, `approval` (décision et journal) et `amount`.
  - `facts` : faits à dire au client.
  - `justified` : faits de justification, et chaque fait cité dans `facts_used` vérifié contre
    les vraies réponses d'outils, désormais enregistrées.
  - `forbidden` : données d'un autre client, dans la réponse au client seulement.
  - `faithful` : aucune action annoncée comme faite si elle attend un humain ou n'a pas eu
    lieu.
- Faits à dire, de justification ou non notés, choisis par l'intérêt du client (`JUSTIFICATION`,
  `UNGRADED`) ; `tickets.jsonl` inchangé.
- Version de la notation figée (`fad6fdab4512`), fixée par un test.
- `evals.run --passes N` : remise à zéro automatique entre les passages, un plafond pour la
  série, variabilité et `pass^k` dans le rapport.
- Juge IA facultatif (`--judge`, haiku, 0,50 $ au plus) : ton et clarté, hors score.
- Rapports versionnés dans `docs/evals/`.
- **Vérifié en conditions réelles** : essai J5.1 5/5. Série « variabilite » (8 tickets × 2
  passages) 16/16, aucun ticket instable, 0,44 $ ; juge 0,01 $.

**Bugs ou surprises**
- L'essai J5.1 passait de 3/5 à 5/5 sans aucune faute de l'agent, une fois la notation
  limitée à ce que le client a besoin de savoir.
- Sur la série « variabilite », 11 échecs sur 16 (11/16) ont été relus : aucun n'était une
  faute de l'agent.
- Le juge IA lisait les dates de la réponse à sa propre date et les croyait incohérentes : 4
  notes basses sur 9 venaient de là.

**Leçons**
1. Noter ce que le client a besoin de savoir, pas tout ce qui fonde la décision. Les faits sont
   à dire (`facts`), de justification (`justified`, vérifiés contre les vraies réponses
   d'outils) ou non notés, choisis par l'intérêt du client.
2. Élargir un *matcher* d'après de vraies réponses, c'est risquer le surajustement. Il faut :
   - relire chaque échec ;
   - n'ajouter que des formulations correctes ;
   - toujours ajouter des tests négatifs (mauvais nombre, phrase niée, mot proche), qui
     montrent qu'une mauvaise réponse échoue encore.
3. La version de la notation est figée (empreinte des règles, fixée par un test). Une
   correction est une nouvelle version, notée dans le journal et dans le rapport ; sinon deux
   scores ne se comparent pas.
4. Un juge IA lit les dates à sa propre date : il faut lui donner la date simulée, sinon il
   pénalise des réponses justes.

### Évaluation complète « baseline » et notation `46f32f478699` (`1889aa4`)

**Fait**
- Série « baseline » : 48 tickets × 2 passages, consigne alors en place (v1), sonnet.
  - 73/96 avec la version figée `fad6fdab4512`, juge 0,06 $
    (`docs/evals/2026-10-28T224853Z-baseline.md`, rapport de référence, inchangé).
  - 81/96 avec la nouvelle version `46f32f478699`
    (`…-baseline-notation-46f32f478699.md`).
- Nouvelle version de la notation, règle par règle, chacune avec ses tests négatifs :
  1. Les jours depuis la livraison sont comptés comme l'outil (jours entamés, `days_since` du
     serveur MCP, heure lue dans `get_shipment`). Cela fait un jour de plus que le calendrier
     pour une livraison du matin (tickets 17, 20, 24).
  2. Un fait cité d'une action exécutée (`refund_order.status`) ou le champ `error` d'une
     lecture en échec est invérifiable (réponse non gardée), et non plus « non lu » (tickets
     17, 18, 30).
  3. Un colis perdu peut être dit « pas (pu être) retrouvé », après le colis ou le transporteur
     (tickets 6, 7).
  4. « Hors délai de 30 jours » vaut « rien n'est dû » (ticket 44).
- Relecture de 8 tickets réussis au hasard : aucune fausse réussite.

**Bugs ou surprises**
- Restaient des erreurs de l'agent :
  - pièges de montant 44 et 45 refusés au lieu d'escalader ;
  - catégorie du ticket 31 ;
  - numéro de commande omis.

  S'y ajoutait le ticket 47 (liste des transporteurs, qu'aucun outil ne donne). Tout cela a été
  traité en J5.3 comme des améliorations de l'agent, sans toucher à la vérité terrain ni à la
  consigne.

### J5.3 : comparaison de configurations, consigne v2 adoptée (`7117f53`)

**Fait**
- Consigne v2 (`agents/support/v2/`), qui corrige seulement les défauts de « baseline » :
  - pièges de montant comparés au montant payé avant le délai (44, 45) ;
  - liste des transporteurs (47) ;
  - numéro de commande toujours cité ;
  - raison d'escalade jamais devinée (36) ;
  - aucun fait sans outil (15) ;
  - ton moins administratif ;
  - délai de suivi de 48 h ouvrées, tiré de `evals/policy.py` (`HUMAN_FOLLOW_UP_HOURS`).
- Versions de la consigne (`support_agent.PROMPTS`) et `evals.run --prompt`, avec la version
  et l'empreinte dans les résultats.
- `python -m evals.compare` :
  - les deux séries renotées avec la même notation ;
  - scores par critère et par catégorie côte à côte ;
  - tickets améliorés et dégradés, coût, juge sur les deux séries ;
  - test de signe exact apparié par ticket.
- **Résultat** (même modèle `claude-sonnet-5-5`, même notation `46f32f478699`) :
  - 81/96 → 87/96 ;
  - `action` et `approval` 92 → 96/96, `facts` 61 → 70/70 ;
  - 7 tickets améliorés (6, 7, 24, 25 : numéro de commande ; 47 : transporteurs ; 45 : escalade
    du piège de montant ; 41), 5 dégradés ;
  - coût identique (2,11 $ et 2,13 $), juge 0,12 $
    (`docs/evals/compare-2026-10-28T224853Z-baseline-vs-2026-10-29T021314Z-prompt-v2.md`).
- **Décision (2026-10-29)** : v2 adoptée comme consigne par défaut du service et de
  l'évaluation, v1 gardée pour comparer. Limites de cette décision :
  1. L'écart global n'est pas significatif (7 ↑ / 5 ↓, p = 0,77). La v2 est adoptée parce que
     les défauts visés sont corrigés de façon stable et sans coût, pas parce qu'un gain est
     démontré.
  2. Le juge ne voit aucun écart (ton 3,6 et clarté 3,9 dans les deux séries), alors que le
     comptage en mesure de nets :
     - « 48 h ouvrées » dans 42 réponses sur 96, contre 0 en v1 ;
     - délais vagues : 33 → 5 ;
     - formules administratives : 14 → 0.

     Le juge n'est pas calibré, et ses notes ne sont pas à surinterpréter.
  3. Défauts restants pour une v3 :
     - catégorie des questions de statut (`delivery_delay` au lieu de `simple_question`,
       tickets 30 et 31) ;
     - faits inventés dans `facts_used` (paraphrase ou champ inexistant, tickets 37 et 40,
       déjà vu au 41 en v1) ;
     - *matcher* de `due_cents` trop étroit : « non prévu par la politique », « livrée il y a
       plus de 30 jours » et « hors délai » seul ne sont pas reconnus. Les tickets 43, 44 et
       45 sont comptés en échec alors que l'escalade est juste. À corriger dans une nouvelle
       version de la notation, avec ses tests négatifs.

**Bugs ou surprises**
- Première série v2 : la limite de dépense de l'API a été atteinte au ticket 9. 40 tickets ont
  été comptés comme des fautes de l'agent, et le passage marqué `completed`. Correction :
  - `sdk_runner` reconnaît une erreur de l'API (`api_error_status`, `AssistantMessage.error`,
    texte « API Error: NNN ») ;
  - `evals.run` arrête la série sur une erreur d'infrastructure (`INFRA_ERRORS`), avec le
    ticket en cause `interrupted`, jamais `failed` ;
  - `evals.grade` et `evals.compare` refusent les anciens fichiers qui comptent ces erreurs
    comme des fautes.
- Le même défaut existe encore dans le service, qui abandonne un ticket après 3 erreurs de
  l'API au lieu de mettre la boucle en pause (limite connue).
- Un écart arrondi à zéro s'affichait « +0,0 » dans le rapport de comparaison : corrigé en
  « 0,0 ».

**Leçons**
- Une erreur d'infrastructure ne doit jamais passer pour une faute de l'agent : elle arrête la
  série.
- Un écart de score ne vaut rien sans son test de significativité.

### J5.4 : intégration continue GitHub Actions (`4997d4d`, `e05ec03`, `5924d8b`, `1d50a1d`)

**Fait**
- Workflows dans `.github/workflows/`.
- `tests.yml` : à chaque push et pull request, `ruff check`, `ruff format --check` puis `pytest`
  complet. PostgreSQL de service, règles d'alerte par les images de Compose. Aucun secret,
  jamais `pull_request_target`, jeton en lecture seule. Badge dans le README.
- `evaluation.yml` : lancée à la main seulement, une seule à la fois.
  - Déroulé : ShopSim à horloge figée, remise à zéro, `evals.run`, puis `evals.grade` avec
    les seuils ; rapport dans le résumé du lancement, résultats et logs en artefacts.
  - Clé d'API dans l'environnement GitHub `evaluation`, donnée à la seule étape `evals.run`.
  - Jeton Chaos tiré au hasard, masqué, jamais dans l'environnement.
  - Paramètres jamais interpolés dans un script.
- Seuils de réussite versionnés (`evals/thresholds.toml`) : score global de 85 %, aucun échec
  admis sur `action`, `approval`, `forbidden` et `faithful`. Code de sortie 3 avec le seuil
  nommé ; une évaluation incomplète échoue toujours.
- Actions épinglées par SHA de commit complet (checkout v7.0.1, setup-uv v10.3.3,
  upload-artifact v7.0.2), version en commentaire, vérifiée par
  `evals/tests/test_workflows.py`.
- Sous-ensemble par défaut porté à 14 tickets, avec les défauts corrigés (44, 45, 47). Règle :
  chaque défaut corrigé a son ticket dans ce sous-ensemble. Plafond par défaut de 2,50 $ pour
  environ 0,35 $ dépensés par passage.
- **Vérifié en conditions réelles**
  - « Tests » vert sur GitHub (toute la suite, alertes comprises).
  - « Évaluation » v2 verte (11/11 sur l'ancien sous-ensemble par défaut).
  - Échec volontaire, v1 sur les tickets 44 et 45 : rouge, avec les seuils `action` et
    `approval` nommés.

**Bugs ou surprises**
- Le premier lancement s'est arrêté en `budget_exhausted` après 0,08 $ sur 1 $. Le deuxième
  ticket ne tenait jamais à côté du premier (isolement + 2 × 0,50 $ de réserve > 1 $) et le
  passage s'arrêtait au lieu d'attendre. Désormais, un ticket attend la fin des exécutions en
  cours ; l'arrêt n'a lieu que si plus rien n'est en cours.
- L'avertissement « Lectures d'outils non enregistrées » s'affichait sur un fichier neuf, à
  cause des tickets jamais lancés (`not_run`, sans lecture par construction). Les lectures
  étaient bien enregistrées.
- Sur GitHub, les « $ » du rapport étaient lus comme des formules (« l e p l u s c h e r »).
  - `\$`, puis `&#36;`, ne suffisaient pas : devant une virgule, un « $ » ouvre quand même une
    formule.
  - Seul `<span>$</span>` marche ; vérifié avec l'API de rendu Markdown de GitHub.
  - Le premier test reposait sur l'hypothèse fausse que `\$` suffisait. Il vérifie maintenant
    chaque ligne du rapport.
- Avec 1 $, deux exécutions ne tenaient jamais en parallèle : plafond porté à 2 $, puis à
  2,50 $ avec les 14 tickets.
- **Limite** : avec 44 et 45 dans le sous-ensemble, la v2 n'a que 12/14 (86 %) sur un passage
  de la série v2, pour un seuil de 85 %, à cause du *matcher* de `due_cents` trop étroit
  (`justified` en échec alors que l'escalade est juste). Un seul autre échec rend la CI rouge.
  À corriger par la nouvelle version de la notation prévue en J5.3.

**Leçons**
- Un plafond se vérifie avec les réserves des exécutions en parallèle, pas seulement avec la
  dépense attendue.
- Un échappement se vérifie sur le vrai rendu, pas sur une hypothèse.

### Notation `8e959d276b3f` : « rien n'est dû » (`due_cents`)

*2026-10-29*

**Fait**
- Raison : le *matcher* de `due_cents` refusait des formulations correctes de la série
  prompt-v2. Des escalades justes des pièges de montant (tickets 43, 44, 45) échouaient donc sur
  `justified`, et la CI était fragile (12/14 sur son sous-ensemble, pour un seuil de 85 %).
- Formulations ajoutées (`evals/facts.py`), chacune reprise d'un motif d'escalade réel :
  - « Dédommagement de retard non prévu par la politique » (43, p1) ;
  - « livrée il y a plus de 30 jours (71 jours) » (44, p2) ;
  - « La livraison date aussi de plus de 30 jours (71) » (44, p1) ;
  - « Livraison il y a 52 jours (hors délai) » (45, p1).
- Le nombre de jours doit dépasser le délai de remboursement (`REFUND_WINDOW_DAYS = 30`, égal
  à `REFUND_WINDOW` de `evals/policy.py`, vérifié par un test), en jours entamés comme
  `days_since` : « plus de 30 jours » et « il y a 31 jours » sont hors délai, « il y a 30
  jours » ne l'est pas.
- « Hors délai » seul n'est admis que juste après « livr… il y a N jours ». Ailleurs, il peut
  parler d'un retard de livraison.
- Tests négatifs, au moins trois par formulation :
  - mauvais nombre de jours (15, 20, 29, 30) ;
  - phrase niée (« pas encore livrée depuis plus de 30 jours », « ne date pas de… », « pas hors
    délai ») ;
  - retard de livraison (« en retard depuis 40 jours (hors délai) », « livré hors délai ») ;
  - autre délai (« hors délai de livraison », « de 15 jours ») ;
  - phrase affirmative ou autre sujet (« prévu par la politique », « livraison non prévue ») ;
  - montant non nul.
- Renotation sans relancer l'agent, dans de nouveaux rapports (les anciens sont gardés) :
  - `docs/evals/2026-10-28T224853Z-baseline-notation-8e959d276b3f.md` : 81/96, inchangé ;
  - `docs/evals/2026-10-29T021314Z-prompt-v2-notation-8e959d276b3f.md` : 87/96 → 91/96, seuils
    de la CI atteints ;
  - `docs/evals/compare-…-prompt-v2-notation-8e959d276b3f.md`, sans juge (le juge ne dépend pas
    de la notation, ses notes restent dans le rapport de comparaison précédent).
- Comparaison v1 → v2 : 8 tickets améliorés, 3 dégradés (30, 37, 40, défauts déjà notés pour
  une v3), toujours non significative (p = 0,227 ; 0,77 avant). Sous-ensemble de la CI : 14/14
  sur les deux passages de v2. Seuils de la CI inchangés.

**Verdicts changés** (comparés ticket par ticket sur les 208 tickets notés, séries « baseline »,
« prompt-v2 » et « variabilite »)
- 4 échecs deviennent des réussites : prompt-v2 p1 tickets 43, 44, 45, et p2 ticket 44.
- Aucune réussite ne devient un échec.
- Baseline p1 44 et p2 45 : leur réponse de refus (« livrée il y a plus de 30 jours ») dit
  maintenant bien que rien n'est dû. Ces tickets restent en échec (catégorie, action,
  validation, `claimed_cents`), à juste titre.

**Leçons**
- Pour décider d'un élargissement, comparer les verdicts ticket par ticket entre les deux
  versions, sur toutes les séries disponibles. Cela montre exactement ce qui change, et prouve
  qu'aucune réussite n'est créée ailleurs.
- La phrase réelle de chaque échec doit être relue avant d'écrire la règle. La liste de départ
  oubliait une quatrième formulation (ticket 44, p1), sans laquelle ce ticket serait resté en
  échec.

## Après J5 — Pause automatique du service sur une erreur d'infrastructure

*2026-10-29*

**Fait**
- Défaut corrigé, noté en J5.3 comme limite connue : dans le service, une erreur
  d'infrastructure (API Anthropic qui refuse : limite de dépense, clé refusée, panne) comptait
  comme un échec du ticket. Après 3 échecs, le ticket était abandonné alors que l'agent n'y
  était pour rien, et la boucle ne faisait que ralentir au lieu de se mettre en pause.
- La catégorie de l'échec est gardée sur chaque exécution (`runs.error_type`). Une erreur de
  `sdk_runner.INFRA_ERRORS` n'est jamais comptée contre le ticket : ni tentative, ni abandon,
  ni délai de relance, ni échec d'affilée.
- API Anthropic, configuration ou CLI (`worker.PAUSE_ERRORS`) : pause automatique de la boucle.
  - Raison `infra`, catégorie dans `control.pause_detail`.
  - Affichée sur `/control` et dans `GET /control/status`, état `paused_infra` de la métrique
    `agentpilot_loop_state`, log `loop.paused`.
  - Sur une exécution de la boucle comme sur une exécution manuelle ; jamais sur une exécution
    d'évaluation, qui a son propre arrêt.
  - Pas de relance automatique : une limite de dépense ou une clé refusée demande un humain,
    comme dans l'évaluation. S'il relance trop tôt, la première exécution remet la boucle en
    pause, sans échec compté.
- ShopSim injoignable pendant une exécution : pas de pause, attente croissante, qui reprend
  seule dès que ShopSim répond.
- Colonnes `runs.error_type` et `control.pause_detail`, et valeur `infra` de `pause_reason`,
  ajoutées à une base existante par `init_db` (pas de base à recréer). Les exécutions d'avant
  (`error_type` NULL) restent comptées comme avant.

- **Vérifié en conditions réelles** (2026-10-29), service lancé avec une clé d'API invalide
  après une remise à zéro :
  - tickets 36 et 48 en `failed`, `error_type=api_unavailable`, 0 $ ; boucle en pause (`infra`,
    `api_unavailable`), aucun ticket abandonné, `agentpilot_loop_state{state="paused_infra"}`
    à 1 ;
  - service relancé avec la vraie clé, boucle relancée : les tickets 36 et 48 repartent les
    premiers, 1 min 42 s après leur échec (comptés en échec, ils auraient attendu le délai de
    relance de 5 min), et réussissent ; 8 autres tickets traités ensuite, tous en `succeeded`.

**Bugs ou surprises**
- Premier essai sans effet : la boucle était dans sa pause de départ, puis, une fois relancée,
  les 11 tickets ouverts restants étaient tous écartés (déjà traités lors d'une session
  précédente). Aucune exécution, donc aucune pause : il fallait d'abord une remise à zéro
  complète.
- Les tickets en échec ont d'abord été devinés d'après l'ordre de la file (4 et 37, à tort) ; les
  logs (`run.finished`, `loop.paused`) ont montré que c'étaient le 36 et le 48.

**Leçons**
- Un essai en conditions réelles part d'un état connu (remise à zéro) ; sinon une absence
  d'effet ne prouve rien.
- Lire les logs plutôt que déduire : `observability.query` filtré sur `run.started`,
  `run.finished` et `loop.paused` donne la preuve directement.

## J6 — Agent Ops et subagents

### J6.1 : serveur MCP Ops en lecture seule (`b0adabf`, puis corrections)

*2026-10-29*

**Fait**
- `mcp_server/ops/` : serveur MCP séparé de celui de l'agent Support, 5 outils en lecture
  seule (`search_logs`, `summarize_logs`, `query_metrics`, `list_alerts`, `check_health`).
  Heure réelle et âges déjà calculés dans chaque réponse ; jamais de chemin, de requête ni
  d'adresse en paramètre ; jamais `chaos.jsonl`, `/chaos` ni jeton.
- Lecture des logs depuis la fin du fichier, liens symboliques refusés.
- Après le premier essai réel : séries sans aucune valeur retirées des métriques ; description
  de `query_metrics` qui prévient que les percentiles de Prometheus sont estimés par tranches.
- Panne `connection_saturation` refaite (modification de ShopSim acceptée) : de 0 à 5 s en
  10 min selon le temps réel écoulé depuis l'activation, plus selon le nombre de requêtes.
- **Vérifié en conditions réelles** (générateur de trafic à 5 req/s, outils appelés depuis
  Claude Code) :
  - état normal : 4 services `ok`, 540 requêtes en 5 min sans 5xx, aucune alerte active ;
  - `db_errors` : logs (516 5xx sur 1 208 requêtes, toutes sur `/orders/{order_id}` et
    `/customers/{customer_id}/orders`, `psycopg.OperationalError`), métriques (100 % de 5xx
    sur ces deux routes, 0 ailleurs), deux alertes `ShopSimHighErrorRate`, résolues 4 min
    20 s après ; aucune mention du Chaos ;
  - `connection_saturation` (ancienne version) : toutes les routes métier à 5 s, `/health` et
    `/clock` intacts, 5 alertes `ShopSimHighLatency` ;
  - `payment_latency` : seul `/payments/{order_id}` à 4 s, les alertes des autres routes
    résolues.
  - `connection_saturation` (nouvelle version, ShopSim reconstruit) : 0,56 s ajoutées à
    1 min, 2,61 s à 5 min, 5,0 s à 10 min, identiques sur toutes les routes métier (attendu :
    0,56, 2,60 et 5 s) ; tendance à 10 min sur `/orders/{order_id}` : 0,010 → 0,038 → 2,49 →
    4,97 s, montée par paliers, `/health` et `/clock` inchangés ; 5 alertes
    `ShopSimHighLatency` après 5 min. Points espacés de 5 min suffisants : pas d'option à
    1 min dans la tendance.

**Bugs ou surprises**
- La saturation atteignait son plafond en une vingtaine de secondes (50 ms de plus par
  requête, à 5 req/s) : vue par points espacés de 5 minutes, elle ressemblait à un saut, et la
  tendance prévue pour distinguer une montée progressive ne montrait rien. Panne irréaliste,
  corrigée dans ShopSim plutôt que de compenser dans l'outil.
- La tendance listait des routes sans aucun trafic, tous leurs points à `null` : retirées.
- Les p95 de Prometheus s'écartent des vrais : 0,24 s pour 0,12 s, 7,4 s pour 5,0 s, 4,9 s
  pour 4,0 s (interpolation dans des tranches larges). Les logs donnent les vraies valeurs.
- `/health` de ShopSim reste vert pendant `db_errors` (`SELECT 1` seulement).
- L'alerte `ShopSimHighLatency` sur `/payments/{order_id}` est restée la même occurrence de la
  saturation à `payment_latency` : la cause a changé sans nouvelle alerte.

- Premier essai de la nouvelle saturation sans effet : ShopSim n'avait pas été reconstruit
  (l'outil en ligne de commande, local, affichait déjà le nouveau message). Un p95 de 0,6 s à
  la première minute a d'abord été lu comme une montée progressive, à tort : c'étaient les
  premières secondes de l'ancienne panne. Prouvé en comparant aux valeurs attendues à 6 min
  (5 s mesurées au lieu de 3 s), puis par le schéma de la table.

**Leçons**
- Vérifier une panne simulée contre ce qu'elle doit montrer, pas seulement qu'elle se
  déclenche : la saturation « marchait », mais ne ressemblait pas à une saturation.
- Vérifier le déploiement avant le comportement : un message affiché par un outil local ne
  prouve pas que le service a la nouvelle version. Comparer chaque mesure à une valeur
  attendue chiffrée, plutôt qu'à une impression (« ça monte »).
- Une panne qui dépend du nombre de requêtes dépend du débit du générateur : la régler sur le
  temps écoulé la rend reproductible.
- Les points d'attention pour l'agent Ops (santé verte pendant une panne de base, signature de
  la saturation, alerte qui survit à un changement de cause, percentiles estimés) sont notés
  sous J6.2 dans CLAUDE.md, pour sa consigne et ses évaluations.

### J6.2 : agent Ops

*2026-10-29 et 2026-10-30*

**Fait**
- `agents/ops/` : consigne d'ingénieur d'astreinte et guide de diagnostic par familles de pannes,
  sans jamais nommer une panne simulée (test de garde-fou).
- Rapport `IncidentReport` validé par un schéma : cause probable sur liste fermée, hypothèses
  avec leur statut, preuves qui citent l'outil, ses arguments, le champ et la valeur lue ; le
  code vérifie aussi l'alerte recopiée et que chaque outil cité a réellement été appelé.
- Lancement par un profil d'agent (`AgentProfile`) : même isolement que l'agent Support, qui
  reste inchangé ; `OpsHooks` refuse et journalise tout outil hors des 5 outils Ops.
- `POST /runs/ops/{alert_id}` et `POST /checks/ops` ; exécutions enregistrées
  (`runs.alert_id`), plafond de 1 $ par enquête, réserve par agent dans le budget du jour.
- **Vérifié en conditions réelles** (modèle `claude-sonnet-5-5`, générateur de trafic) :
  - isolement : consigne restituée (causes, niveaux de confiance, outils, heure), les 5
    questions pièges à `null` ;
  - `db_errors` : `database_errors`, confiance élevée, les deux routes touchées ; 5 appels,
    0,06 $ ; il signale lui-même que la santé verte est un piège connu ;
  - `connection_saturation` : `resource_saturation`, confiance moyenne ; montée par paliers et
    même plafond sur toutes les routes métier, `/health` et `/clock` rapides ; il écarte les
    erreurs de base de la panne précédente (dernière il y a 13 min, taux actuel nul) ; 4 appels,
    0,06 $ ;
  - `payment_latency` : `slow_dependency`, confiance moyenne, seule `/payments/{order_id}`
    touchée ; il resserre de lui-même sa fenêtre de 15 à 3 minutes pour séparer la panne
    actuelle de la saturation précédente, donne la valeur exacte des logs (4,01 s) plutôt que
    l'estimation de Prometheus, et recommande de chercher à part la cause de l'ancienne lenteur ;
    6 appels, 0,07 $.
  - Toutes les preuves citées (34 au total : 10, 13 et 11) recoupées avec les réponses
    d'outils gardées au journal : toutes exactes. Aucun appel hors des 5 outils Ops.

**Bugs ou surprises**
- Sans `LOG_DIR`, le serveur MCP Ops aurait cherché les logs dans son dossier de travail vide,
  sans erreur : il le reçoit maintenant toujours, en absolu (trouvé par un test).
- Une valeur ajoutée à une énumération PostgreSQL ne peut pas servir dans la transaction qui
  l'ajoute : `init_db` valide les ajouts d'énumérations avant de créer l'index qui les utilise.
- Défauts relevés dans les rapports, à reprendre en J6.3 et dans une révision de la consigne :
  - la boucle d'AgentPilot en pause manuelle (normal) citée comme point à vérifier ;
  - preuve faible pour écarter une erreur applicative (nombre d'exceptions au lieu de leur
    type) ;
  - résumé de la saturation avec la p95 estimée de Prometheus (6,6 s) au lieu de la valeur
    exacte des logs (5,0 s), alors que la consigne prévient de ce piège ;
  - champ `observed` rempli d'horodatages au lieu d'âges.

**Leçons**
- Recouper chaque preuve d'un rapport avec la réponse d'outil gardée au journal : c'est ce qui
  permet d'affirmer que les preuves sont exactes, pas seulement plausibles. À automatiser en
  J6.3.
- Les pièges notés en vérifiant J6.1 ont servi : l'agent les cite et les évite, sauf un.

### J6.3 : évaluation de l'agent Ops

*2026-10-30*

**Fait**
- Vérité terrain des incidents (`evals/incidents.jsonl`) : 6 scénarios (les 4 pannes, dont
  `bad_deploy` avec des remboursements envoyés par l'évaluation ; saturation puis paiements
  lents sur la même alerte ; fausse alerte), jamais à portée de l'agent (tests).
- Lanceur autonome (`python -m evals.ops_run`), gardien et charge de remboursements dans leurs
  propres processus ; notation par du code (`python -m evals.ops_grade`), version des règles
  `a10b81ce62bc`, preuves recoupées avec les vraies réponses d'outils du journal.
- **Vérification réelle** sur `db_errors` et `false_alert` (0,13 $,
  `docs/evals/ops-2026-10-30T003842Z-verif.md`) : le lanceur a déroulé seul les deux scénarios
  (panne activée, alerte trouvée 2 min 20 s plus tard, enquête, panne arrêtée) ; deux causes
  justes (`database_errors` avec une confiance élevée, `no_incident` sur la fausse alerte) ;
  19 preuves exactes, 2 retrouvées, **2 introuvables** sur `db_errors`, d'où 1 / 2 scénarios
  réussis.
- **Arrêt en cours de scénario vérifié** : Ctrl+C pendant `db_errors` (panne active depuis
  1 min 30 s) ; panne désactivée, scénario et passage `interrupted`, résultats enregistrés,
  plus aucun processus (lanceur, AgentPilot, générateur, gardien) ; seul ShopSim reste, dans
  Docker.
- **Série de référence** avec la nouvelle consigne (`bf42e5b6656d`), les 6 scénarios, un
  passage (`docs/evals/ops-2026-10-30T012850Z-reference.md`, commit `b5d7ab2`) : **6 / 6**,
  aucune faute grave, **60 preuves exactes sur 60** (aucune introuvable : la section
  « Preuves » a supprimé les citations fautives, sur ce passage du moins). Confiance `high`
  sur `db_errors` et `bad_deploy`, `medium` ailleurs. 0,38 $, 13 à 19 s et 6 à 10 tours par
  enquête ; 1 h 46 pour la série. Aucune perturbation : trafic continu (~300 requêtes par
  minute, aucune minute vide dans les logs de ShopSim) et écarts du journal du lanceur
  conformes à la vérité terrain.

**Bugs ou surprises**
- Les 2 preuves introuvables n'étaient pas inventées, mais mal citées : deux champs réunis en
  une preuve (`warnings[http.exception].error_type / count` = « psycopg.OperationalError /
  463 », deux valeurs justes) ; et un mauvais index avec un texte coupé (`lines[0].error` au
  lieu de `lines[1].error`, message tronqué à sa première ligne). La notation a raison de
  refuser une preuve qu'on ne peut pas retrouver, mais le mot « fausse » exagérait.
- En codant : un argument omis par une preuve (`limit`) faisait refuser une preuve juste ; un
  Ctrl+C pendant le lancement de la charge pouvait l'oublier ; le gardien prenait un processus
  « zombie » pour vivant. Corrigés, chacun avec son test.

**Décisions**
- **Consigne de l'agent Ops modifiée** (2026-10-30), après la vérification : une section
  « Preuves » exige un champ et une valeur par preuve, la valeur recopiée en entier, le chemin
  et l'index tels que la réponse les donne, et les arguments de l'appel. Empreinte de la
  consigne (`agents/ops/`) : **`0f55381777a7` → `bf42e5b6656d`**. Les prochaines évaluations
  sont à comparer à cette version, pas à la vérification ci-dessus (`0f55381777a7`).
- Le rapport dit « introuvable » au lieu de « fausse » (même règle : le critère échoue). La
  notation n'est pas desserrée : pas de préfixe accepté, pas de champs réunis découpés.

**Leçons**
- Une preuve vérifiable par du code doit être atomique : un champ, une valeur entière. C'est à
  la consigne de l'exiger, pas à la notation de deviner.
- Une référence à 100 % ne laisse qu'un sens possible au score : J6.4 (subagents) devra
  comparer aussi le coût, la durée, les tours et la stabilité sur plusieurs passages, ou
  ajouter des scénarios plus difficiles (nouvelle version de la vérité terrain).

### J6.4 (première partie) : scénarios Ops plus difficiles

*2026-10-30*

**Pourquoi**
- La référence de J6.3 est à 6 / 6, avec 60 preuves exactes : l'évaluation ne départageait
  plus rien, et ne pourrait pas montrer ce qu'apportent des subagents.

**Fait**
- 5 nouveaux scénarios, chacun pour une compétence de raisonnement (les 6 de J6.3 restent,
  même contenu) :

  | Scénario | Compétence | Cause attendue |
  |---|---|---|
  | `db_and_payments` (2 enquêtes) | attribuer à chaque alerte sa cause quand deux pannes de natures différentes coexistent | `database_errors` et `slow_dependency` |
  | `db_and_bad_deploy` | attribuer des 5xx par le type d'erreur, alors que la base produit le plus d'erreurs | `application_error` |
  | `db_intermittent` | ne pas écarter une panne partielle (15 % d'erreurs, p50 normale, santé verte) | `database_errors` |
  | `saturation_then_bad_deploy` | une panne qui commence pendant qu'une autre se résout, au milieu des alertes de latence | `application_error` |
  | `db_errors_ended` | une alerte encore active, mais des erreurs déjà finies : regarder toute la période | `database_errors` |

- Nouvelle panne `db_errors_intermittent` (accord pour modifier ShopSim) : même erreur que
  `db_errors` sur 15 % des lectures des commandes, et ajout de la valeur au type
  `chaos_scenario` d'une base existante (`init_db`).
- Plusieurs enquêtes par scénario : la vérité terrain donne une liste d'enquêtes, chacune avec
  son alerte, sa cause et ses routes ; le lanceur les fait l'une après l'autre, et la notation
  note chaque enquête (« scénario · enquête ») contre ses propres attentes.

**Décisions**
- **Routes de l'autre panne interdites** (ni attendues ni admises) et signalées
  « autre panne » dans le motif. Un test vérifie, pour chaque scénario à deux pannes, que ces
  routes ne sont jamais attendues ni admises.
- **Consigne de l'agent Ops précisée** : « les routes touchées par l'incident de l'alerte de
  départ » ; un autre incident visible se signale dans le résumé ou une hypothèse, sans mêler
  ses routes. Empreinte de la consigne : **`bf42e5b6656d` → `9704eed2dc32`**.
- **Nouvelle référence sur les 11 scénarios** avec cette consigne, pour une seule référence
  cohérente ; la série de J6.3 (`bf42e5b6656d`) n'est plus la référence.
- **Versions** : vérité terrain `a05ce017d47d` (J6.3, 6 scénarios) → **`1d0c1c3e153b`**
  (11 scénarios), désormais figée par un test (`FROZEN_TRUTH_SHA256`) ; notation
  `a10b81ce62bc` → **`11a2215b0643`** (mêmes critères, attentes par enquête, motif
  « autre panne »). Deux rapports de versions différentes ne se comparent pas.
- **Écarté** : une fausse alerte pendant une vraie panne ailleurs. Le guide réserve
  `no_incident` au cas où tout est normal « sur la route de l'alerte et ailleurs » : la bonne
  réponse serait ambiguë, donc le scénario injuste.
- 15 % d'erreurs plutôt que 10 % pour `db_intermittent` : l'alerte (5xx > 5 % sur 2 min)
  doit se déclencher sûrement, sinon le scénario finit `no_alert`, non noté.

**Nouvelle référence : `reference-v2`** (`docs/evals/ops-2026-10-30T131434Z-reference-v2.md`,
commit `3d95980`, consigne `9704eed2dc32`, notation `11a2215b0643`, vérité terrain
`1d0c1c3e153b`)
- Contrôle préalable de `db_errors_intermittent` avec le générateur : 5xx sur les deux routes
  des commandes seulement (11 à 14 % mesurés, petits échantillons), toutes en
  `psycopg.OperationalError`, durées normales.
- **12 / 12 enquêtes réussies**, aucune faute grave, **142 preuves exactes sur 142**. 0,78 $
  (0,044 à 0,082 $ par enquête), 13 à 20 s et 6 à 10 tours par enquête, 3 h 02 pour la série.
  Confiance `high` sur 5 enquêtes, `medium` sur 7.
- Les pièges des nouveaux scénarios sont vus : chaque alerte de `db_and_payments` reçoit sa
  cause, l'autre panne signalée « distincte » sans ses routes ; `KeyError` sur `/refunds`
  malgré l'incident de base ; panne partielle ni écartée ni prise pour `no_incident` ;
  erreurs « qui semblent avoir cessé il y a ~1 min 35 s » sans conclure `no_incident`.
- Les scénarios plus difficiles ne départagent toujours rien : l'agent seul est à 100 %.

**Limites et défaut non noté**
- **Scénarios non indépendants** : l'agent lit des fenêtres de 15 min ou plus et voit les
  restes des scénarios précédents (« une ancienne panne /refunds », « une lenteur
  antérieure »). Sans effet ici, mais un résultat peut dépendre de l'ordre des scénarios.
- **Un seul passage** : 12 / 12 ne dit rien de la stabilité.
- **Inexactitude du résumé, non notée** : sur `saturation_then_bad_deploy`, le résumé dit
  qu'une lenteur « touche » encore les autres routes, alors que la saturation avait cessé
  3 min plus tôt. La cause, les routes et les preuves sont justes ; la notation ne lit pas
  le résumé, donc ce défaut n'est pas compté.

**Suite décidée** : J6.4 en expérience courte, un enquêteur logs et un enquêteur métriques,
un passage sur les 11 scénarios, comparé à `reference-v2` sur le coût, la durée, les tours et
les régressions. Le score ne peut pas progresser ; sans gain sur le reste, les subagents ne
seront pas retenus, avec les chiffres à l'appui.

### J6.4 (seconde partie) : l'agent Ops avec subagents, en expérience courte

*2026-10-30*

**Règle de décision, écrite avant l'essai** : les subagents sont retenus seulement s'ils sont
**sans régression** (aucune enquête réussie par `reference-v2` et ratée, aucune faute grave ni
preuve introuvable de plus) **et** moins chers ou plus rapides. Sinon : « essayé, pas
retenu », chiffres à l'appui. `evals.ops_compare` calcule ce verdict ; la décision reste
humaine.

**Fait**
- Architecture `subagents` (réglage `AGENTPILOT_OPS_ARCHITECTURE`, `solo` par défaut, le
  service inchangé sans lui) :
  - l'enquêteur principal garde `list_alerts` et `check_health` et délègue par l'outil
    `Agent` ;
  - `logs-investigator` lit les logs (`search_logs`, `summarize_logs`) ;
  - `metrics-investigator` lit les métriques (`query_metrics`).
  - Même modèle partout. Consignes dans `agents/ops-subagents/` : `agents/ops/` ne change pas,
    l'empreinte de l'agent seul reste `9704eed2dc32`.
- Isolement, demandé en plus du plan :
  - **l'outil `Agent` ne lance que les deux subagents prévus** : tout autre type (agent
    généraliste avec fichiers ou shell, type absent ou inconnu) est refusé par les hooks et
    journalisé ;
  - chaque agent est borné à ses outils (champ `agent_type` des hooks) : un subagent n'a ni
    fichiers, ni shell, ni délégation, ni les outils de l'autre.
  - `POST /checks/ops` exige que les deux subagents soient réellement lancés (comptés par les
    hooks), qu'ils restituent leurs seuls outils, ni fichier ni shell, et aucune réponse
    piège.
- Auteur de chaque appel au journal (`tool_calls.agent`), recopié dans les résultats de
  l'évaluation.
- `evals.ops_run --architecture`, et `evals.ops_compare` (régressions, coût, durée, tours,
  appels par agent, verdict).

**Limite connue** : la liste des outils d'un subagent, dans la vérification d'isolement, est
celle qu'il déclare. La garantie vient de sa définition (`AgentDefinition.tools`, testée) et
des hooks, qui refusent tout le reste (testés, et prouvés à chaque enquête par le journal).

**Vérification d'isolement réelle (`POST /checks/ops`, mode `subagents`)** : réussie à la
quatrième tentative (0,06 $ ; 0,08 $ pour la troisième). Les trois premières ont montré trois
comportements de la CLI (Claude Code 2.1.310) que le faux SDK des tests ne pouvait pas voir :
1. **`Task` est un alias de `Agent`**, et la CLI compare les outils interdits au nom **et** aux
   alias : interdire `Task` retirait `Agent` (« outils manquants : Agent »). Les deux noms
   sont retirés des interdits en mode `subagents` seulement (`BUILTIN_ALIASES`).
2. **À l'initialisation, l'outil est annoncé sous son ancien nom `Task`** : ramené à `Agent`
   avant le contrôle (`canonical_tools`) ; en mode `solo`, `Agent` comme `Task` restent
   refusés.
3. **Les subagents partent par défaut en arrière-plan** (`async_launched`) : l'enquêteur
   principal a fini en 2 tours, sans leurs réponses ni sortie structurée.
   `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` les garde au premier plan (`background=False`
   dans la définition ne suffit pas). Un lancement en arrière-plan ne compte plus comme
   preuve dans la vérification.

Résultat : outils annoncés `Agent`, les 5 outils Ops et la sortie structurée, rien d'autre ;
les deux subagents lancés au premier plan (`completed`, aucun appel d'outil), chacun avec
ses seuls outils, ni fichier ni shell, les cinq questions pièges à `null`, de même pour
l'enquêteur principal. Reste à voir en enquête réelle : le champ `agent_type` des hooks pour
les appels des subagents (s'il manquait, leurs lectures seraient refusées comme celles de
l'enquêteur principal : l'enquête échouerait, sans rien laisser passer).

**Essai sur un scénario** (`db_errors`, 0,14 $) : 1 / 1, 15 preuves exactes sur 15. En réel,
chaque appel porte son auteur (`logs-investigator` pour les logs, `metrics-investigator` pour
les métriques, l'enquêteur principal pour `check_health`) : `agent_type` arrive bien aux
hooks. Les deux subagents tournent en parallèle ; leurs lancements `Agent` sont inscrits au
journal après leurs appels (le hook ne les voit qu'une fois le subagent terminé).

**Série `subagents`** (`docs/evals/ops-2026-10-30T202002Z-subagents.md`, commit `b51fa59`,
configuration `c298c7325064`, un passage sur les 11 scénarios) et comparaison à
`reference-v2` (`docs/evals/ops-compare-2026-10-30T131434Z-reference-v2-vs-2026-10-30T202002Z-subagents.md`) :

| | Agent seul (`reference-v2`) | Subagents | Écart |
|---|---|---|---|
| Enquêtes réussies | 12 / 12 | 11 / 12 | 1 régression |
| Fautes graves | 0 | 0 | |
| Coût | 0,78 $ | 1,75 $ | +125 % |
| Durée des enquêtes | 201 s | 387 s | +93 % |
| Tours de l'agent principal | 88 | 64 | −27 % |
| Appels aux outils de lecture | 64 | 97 | +52 % |

- Plus cher et plus lent sur **chacune** des 12 enquêtes (2 à 2,8 fois le coût). Moins de
  tours pour l'enquêteur principal, mais plus de travail au total : 33 appels de
  `logs-investigator`, 49 de `metrics-investigator`, 15 du principal.
- **Régression sur `saturation_then_payments`** : `resource_saturation` (confiance `medium`)
  au lieu de `slow_dependency`, et les 4 routes de la saturation citées en trop. C'est le
  piège nommé par la consigne (« une alerte peut survivre à un changement de cause ») :
  l'agent écrit lui-même que seule `/payments` reste lente maintenant (4,875 s), mais
  l'attribue à « la fin de cet épisode » et laisse `slow_dependency` non tranchée.
- Preuves : aucune introuvable, la recopie d'un agent à l'autre ne les déforme pas ; mais
  13 seulement retrouvées ailleurs dans la réponse et 5 invérifiables (agent seul : 142
  exactes sur 142).

**Décision : essayé, pas retenu** (verdict de la règle fixée avant l'essai, validé). Le
surcoût suffit à lui seul : sans la régression, la règle aurait aussi donné « pas retenu ».
Le code reste, `solo` par défaut ; le mode `subagents` reste disponible pour un autre essai
(par exemple des subagents sur un modèle plus petit), et `evals.ops_compare` resservira.

**Hypothèse sur la régression, non prouvée (un seul passage)** : une perte de contexte à la
délégation. Les subagents résument une tendance (« montée par paliers sur toutes les
routes ») sans connaître les pièges de la consigne ni le guide de diagnostic ; l'enquêteur
principal raisonne sur ces résumés plutôt que sur les valeurs, et ne voit plus la
différence entre « pendant » et « maintenant ». L'agent seul avait les valeurs brutes et les
pièges dans le même contexte.

**Leçons**
- Déléguer la lecture coûte deux fois : le contexte de chaque subagent, puis la synthèse.
  Sur des enquêtes courtes (15 à 20 s, 6 à 10 tours), il n'y a rien à paralléliser qui
  paie ce coût.
- Un subagent qui résume perd ce qui distingue deux causes proches : ici, le moment.
- La CLI a trois comportements invisibles au faux SDK (alias `Task`, nom annoncé à
  l'initialisation, lancement en arrière-plan) : seule une vérification réelle les montre.

