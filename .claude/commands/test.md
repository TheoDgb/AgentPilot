---
description: Lance les tests et analyse les échecs sans rien modifier
argument-hint: [chemin optionnel, ex. shopsim/]
allowed-tools: Bash(.venv/bin/python -m pytest:*), Read, Grep, Glob
---

Lance `pytest $ARGUMENTS -q` (tout le projet si aucun chemin n'est donné).

Ensuite :
1. Résume le résultat : nombre de tests passés, échoués, ignorés.
2. Pour chaque échec, lis le code concerné et identifie la cause probable.
3. Propose une correction précise par échec, fichier et ligne à l'appui.
4. Signale tout problème de configuration (pytest, dépendances, venv).

Ne modifie aucun fichier. Termine en demandant quelles corrections appliquer.