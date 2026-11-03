#!/bin/sh
# Démarrage d'Alertmanager (J7.2), lancé par docker-compose.yml avec `sh` (image busybox).
#
# AGENTPILOT_DISCORD_WEBHOOK_URL définie, adresse d'un webhook Discord : écrite dans un fichier
# lisible du seul utilisateur d'Alertmanager, retirée de l'environnement, puis configuration
# avec le canal Discord (alertmanager-discord.yml, `webhook_url_file`). Vide : configuration
# sans Discord (alertmanager.yml), exactement comme avant J7.2. Adresse d'un autre site :
# refusée (jamais envoyée ailleurs), configuration sans Discord. La valeur n'est jamais affichée.
set -eu

CONFIG_DIR="${ALERTMANAGER_CONFIG_DIR:-/etc/alertmanager}"
URL_FILE="${DISCORD_URL_FILE:-/tmp/discord_webhook_url}"
URL="${AGENTPILOT_DISCORD_WEBHOOK_URL:-}"
unset AGENTPILOT_DISCORD_WEBHOOK_URL
CONFIG="$CONFIG_DIR/alertmanager.yml"
rm -f "$URL_FILE"

case "$URL" in
  "") ;;
  https://discord.com/api/webhooks/* | https://discordapp.com/api/webhooks/*)
    umask 077
    printf '%s' "$URL" > "$URL_FILE"
    CONFIG="$CONFIG_DIR/alertmanager-discord.yml"
    ;;
  *)
    echo "AGENTPILOT_DISCORD_WEBHOOK_URL n'est pas l'adresse d'un webhook Discord : canal désactivé." >&2
    ;;
esac
URL=

exec alertmanager --config.file="$CONFIG" "$@"
