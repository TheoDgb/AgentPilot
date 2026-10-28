"""Générateur de trafic (J4.3) : des clients simulés font vivre ShopSim.

- Parcours de clients (`visits`) : compte, commandes, paiements, livraisons, à un débit réglable
  en boucle ouverte (`pacer`), comme de vrais clients qui n'attendent pas les autres.
- Nouveaux tickets (`tickets`), désactivés par défaut : chaque ticket est traité par l'agent
  Support et coûte de l'argent. Textes des modèles du seed, cohérents avec les données (le
  client parle de ses propres commandes). Pas de vérité terrain : hors évaluation.

Uniquement l'API publique de ShopSim, par une liste blanche de routes (`client`) : jamais Chaos,
jamais les actions (remboursement, renvoi, réponse, escalade, résolution).

Bibliothèque standard seulement (urllib, threads) : tourne aussi dans l'image de ShopSim.
"""
