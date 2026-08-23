#!/bin/bash
# Sorveglianza oraria della casella TPL del Comune di Imperia.
#
# Installazione (utente non privilegiato, senza diritti di amministratore):
#   crontab -e
#   17 * * * * /opt/tpl-inbox-watch/tpl-inbox-watch.sh
#
# Configurazione in ~/.config/tpl-inbox-watch/env (permessi 0600).

set -u

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${TPL_ENV_FILE:-${HOME}/.config/tpl-inbox-watch/env}"
LOG_FILE="${HOME}/tpl-inbox-watch.log"
LOCK_FILE="/tmp/tpl-inbox-watch.lock"

log() { echo "[$(date -Iseconds)] $*" >> "${LOG_FILE}"; }

if [[ ! -r "${ENV_FILE}" ]]; then
    log "ERRORE: configurazione assente o illeggibile (${ENV_FILE})"
    exit 1
fi

# Un solo giro per volta: se la casella risponde lentamente il cron successivo
# non si somma al precedente.
if [[ -f "${LOCK_FILE}" ]]; then
    LOCK_PID="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
    if [[ -n "${LOCK_PID}" ]] && kill -0 "${LOCK_PID}" 2>/dev/null; then
        log "SALTATO: esecuzione gia' in corso (PID ${LOCK_PID})"
        exit 0
    fi
    rm -f "${LOCK_FILE}"
fi
echo $$ > "${LOCK_FILE}"
trap 'rm -f "${LOCK_FILE}"' EXIT

set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a

python3 "${BASE_DIR}/main.py" "$@" >> "${LOG_FILE}" 2>&1
ESITO=$?
[[ ${ESITO} -ne 0 ]] && log "ERRORE: uscita con codice ${ESITO}"
exit 0
