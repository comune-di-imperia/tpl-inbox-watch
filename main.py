#!/usr/bin/env python3
"""
Sorveglianza della casella di posta della sperimentazione TPL a guida autonoma
del Comune di Imperia.

Controlla a intervalli regolari la casella che raccoglie le risposte dei
cittadini all'applicazione della sperimentazione. Quando arrivano messaggi
nuovi rispetto al giro precedente invia una email di riepilogo e, se
configurata, una notifica Telegram.

Lo stato conserva l'UID piu' alto gia' notificato: i messaggi si confrontano
per UID e non per data, perche' la data e' quella dichiarata dal mittente e non
riflette necessariamente l'ordine di arrivo.

Configurazione: variabili d'ambiente, tipicamente da /etc/tpl-inbox-watch/env
(vedi env.example). Nessuna credenziale risiede nel codice.

Uso:
    python3 main.py               # controllo e notifica
    python3 main.py --dry-run     # elenca i messaggi nuovi senza notificare
    python3 main.py --show        # mostra lo stato salvato

Copyright (c) 2026 Comune di Imperia.
Distribuito con licenza EUPL-1.2.
"""

import argparse
import email
import email.utils
import html
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger("tpl_inbox_watch")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def env(nome: str, default: str = "") -> str:
    return os.environ.get(nome, default).strip()


class Config:
    """Parametri di esecuzione, letti dall'ambiente."""

    def __init__(self) -> None:
        self.imap_host = env("TPL_IMAP_HOST", "mail.comune.imperia.it")
        self.imap_port = int(env("TPL_IMAP_PORT", "993"))
        self.imap_user = env("TPL_IMAP_USER")
        self.imap_password = env("TPL_IMAP_PASSWORD")
        self.imap_folder = env("TPL_IMAP_FOLDER", "INBOX")

        self.smtp_host = env("TPL_SMTP_HOST", "mail.comune.imperia.it")
        self.smtp_port = int(env("TPL_SMTP_PORT", "587"))
        self.smtp_user = env("TPL_SMTP_USER") or self.imap_user
        self.smtp_password = env("TPL_SMTP_PASSWORD") or self.imap_password

        self.mittente = env("TPL_EMAIL_FROM") or self.imap_user
        self.mittente_nome = env("TPL_EMAIL_FROM_NAME", "Casella TPL Imperia")
        self.destinatari = self._lista("TPL_EMAIL_TO")
        self.copia = self._lista("TPL_EMAIL_CC")

        self.telegram_token = env("TPL_TELEGRAM_TOKEN")
        # Piu' destinatari: ognuno deve avere aperto una conversazione col bot,
        # altrimenti Telegram rifiuta l'invio verso quel chat_id.
        self.telegram_chat_id = self._lista("TPL_TELEGRAM_CHAT_ID")

        self.ignora_mittenti = [
            m.lower() for m in self._lista("TPL_IGNORA_MITTENTI") if m
        ]
        # Caratteri di anteprima del corpo. A 0 il corpo non viene nemmeno letto.
        self.lunghezza_anteprima = int(env("TPL_LUNGHEZZA_ANTEPRIMA", "256"))
        self.state_file = Path(
            env("TPL_STATE_FILE", str(Path.home() / ".local/state/tpl-inbox-watch.json"))
        )

        # I destinatari possono essere gestiti dall'interfaccia web: se il file
        # esiste ha la precedenza sulle variabili d'ambiente, che restano come
        # configurazione di riserva per chi installa senza applicazione.
        self._applica_destinatari()

    def _applica_destinatari(self) -> None:
        """Sostituisce i destinatari con quelli gestiti dall'interfaccia.

        Il file lo scrive l'applicazione web e contiene solo indirizzi e
        identificativi di chat: nessuna credenziale, quindi puo' stare in una
        cartella condivisa fra le due utenze. Se manca o e' illeggibile si
        prosegue con le variabili d'ambiente, perche' restare senza
        destinatari significherebbe non avvisare piu' nessuno.
        """
        percorso = Path(
            env(
                "TPL_DESTINATARI_FILE",
                str(self.state_file.parent / "destinatari.json"),
            )
        )
        if not percorso.exists():
            return

        try:
            dati = json.loads(percorso.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Elenco destinatari illeggibile: uso la configurazione di riserva",
                extra={"context": {"file": str(percorso)}},
            )
            return

        email_voci = [v for v in dati.get("email", []) if v.get("indirizzo")]
        principali = [v["indirizzo"] for v in email_voci if v.get("ruolo", "a") == "a"]
        copie = [v["indirizzo"] for v in email_voci if v.get("ruolo") == "cc"]

        if principali:
            self.destinatari = principali
            self.copia = copie
        elif email_voci:
            logger.warning(
                "Nessun destinatario principale nell'elenco: uso la configurazione "
                "di riserva"
            )

        self.telegram_chat_id = [
            str(v["chat_id"]) for v in dati.get("telegram", []) if v.get("chat_id")
        ]

    @staticmethod
    def _lista(nome: str) -> list:
        grezzo = env(nome)
        return [v.strip() for v in grezzo.replace(";", ",").split(",") if v.strip()]

    def valida(self) -> list:
        mancanti = []
        if not self.imap_user:
            mancanti.append("TPL_IMAP_USER")
        if not self.imap_password:
            mancanti.append("TPL_IMAP_PASSWORD")
        if not self.destinatari:
            mancanti.append("TPL_EMAIL_TO")
        return mancanti


def decodifica(valore: str) -> str:
    """Rende leggibile un header MIME codificato (RFC 2047)."""
    if not valore:
        return ""
    try:
        return str(make_header(decode_header(valore)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return valore


def _testo_parte(parte) -> str:
    """Testo di una singola parte MIME, con il suo charset dichiarato."""
    contenuto = parte.get_payload(decode=True)
    if not contenuto:
        return ""
    charset = parte.get_content_charset() or "utf-8"
    try:
        return contenuto.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return contenuto.decode("utf-8", errors="replace")


def estrai_anteprima(messaggio, limite: int) -> str:
    """Prime righe del messaggio, ridotte a testo semplice.

    Si preferisce la parte `text/plain`; se manca si ripiega sull'HTML privato
    dei tag. Gli allegati vengono ignorati.
    """
    grezzo = ""
    if messaggio.is_multipart():
        for parte in messaggio.walk():
            if parte.get_content_maintype() == "multipart":
                continue
            if "attachment" in (parte.get("Content-Disposition") or "").lower():
                continue
            if parte.get_content_type() == "text/plain":
                grezzo = _testo_parte(parte)
                break
        if not grezzo:
            for parte in messaggio.walk():
                if parte.get_content_type() == "text/html":
                    grezzo = re.sub(r"<[^>]+>", " ", _testo_parte(parte))
                    break
    else:
        grezzo = _testo_parte(messaggio)
        if messaggio.get_content_type() == "text/html":
            grezzo = re.sub(r"<[^>]+>", " ", grezzo)

    grezzo = html.unescape(grezzo)
    # Le righe di citazione della conversazione precedente non aggiungono nulla.
    utili = [
        r.strip()
        for r in grezzo.splitlines()
        if r.strip() and not r.lstrip().startswith(">")
    ]
    testo = " ".join(utili)
    testo = re.sub(r"\s+", " ", testo).strip()

    if len(testo) <= limite:
        return testo
    return testo[:limite].rstrip() + "…"


def carica_stato(percorso: Path) -> dict:
    if not percorso.exists():
        return {}
    try:
        return json.loads(percorso.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "Stato illeggibile, riparto da zero",
            extra={"context": {"file": str(percorso)}},
        )
        return {}


def salva_stato(percorso: Path, stato: dict) -> None:
    percorso.parent.mkdir(parents=True, exist_ok=True)
    tmp = percorso.with_suffix(".tmp")
    tmp.write_text(json.dumps(stato, indent=1), encoding="utf-8")
    tmp.replace(percorso)


def leggi_nuovi(cfg: Config, ultimo_uid: int) -> tuple:
    """Messaggi con UID superiore all'ultimo notificato.

    Ritorna (elenco messaggi, UID massimo presente in casella).
    """
    contesto = ssl.create_default_context()
    with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=contesto) as imap:
        imap.login(cfg.imap_user, cfg.imap_password)
        imap.select(cfg.imap_folder, readonly=True)

        esito, dati = imap.uid("SEARCH", None, "ALL")
        if esito != "OK" or not dati or not dati[0]:
            return [], ultimo_uid

        uids = [int(u) for u in dati[0].split()]
        massimo = max(uids) if uids else ultimo_uid

        messaggi = []
        for uid in sorted(u for u in uids if u > ultimo_uid):
            # BODY.PEEK non tocca il flag \Seen: chi apre la casella trova i
            # messaggi ancora da leggere. Senza anteprima si scaricano i soli
            # header, che su messaggi con allegati pesanti fa una bella
            # differenza.
            porzione = (
                "(BODY.PEEK[])"
                if cfg.lunghezza_anteprima > 0
                else "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            esito, dati = imap.uid("FETCH", str(uid), porzione)
            if esito != "OK" or not dati or not dati[0]:
                continue

            completo = email.message_from_bytes(dati[0][1])
            mittente = decodifica(completo.get("From", ""))
            if any(ign in mittente.lower() for ign in cfg.ignora_mittenti):
                continue

            messaggi.append(
                {
                    "uid": uid,
                    "data": decodifica(completo.get("Date", "")),
                    "da": mittente,
                    "oggetto": decodifica(completo.get("Subject", ""))
                    or "(senza oggetto)",
                    "anteprima": (
                        estrai_anteprima(completo, cfg.lunghezza_anteprima)
                        if cfg.lunghezza_anteprima > 0
                        else ""
                    ),
                }
            )

    return messaggi, massimo


def componi_email(cfg: Config, messaggi: list) -> EmailMessage:
    righe_html = ""
    righe_testo = ""
    for m in messaggi:
        anteprima = m.get("anteprima", "")
        anteprima_html = (
            f"<br><span style='color:#555'>{html.escape(anteprima)}</span>"
            if anteprima
            else ""
        )
        righe_html += (
            "<li style='margin-bottom:10px'>"
            f"<span style='color:#7f8c8d'>{html.escape(m['data'])}</span><br>"
            f"da:&nbsp; {html.escape(m['da'])}<br>"
            f"ogg: <b>{html.escape(m['oggetto'])}</b>"
            f"{anteprima_html}</li>"
        )
        righe_testo += f" • {m['data']}\n   da:  {m['da']}\n   ogg: {m['oggetto']}\n"
        if anteprima:
            righe_testo += f"   txt: {anteprima}\n"

    quanti = len(messaggi)
    plurale = "nuovi messaggi" if quanti > 1 else "nuovo messaggio"
    verbo = "ci sono" if quanti > 1 else "c'e'"

    testo = (
        "Buongiorno,\n\n"
        f"{verbo} {quanti} {plurale} da gestire nella casella della sperimentazione "
        f"{cfg.imap_user}.\n\n"
        f"== Da gestire ({quanti}) ==\n{righe_testo}\n"
        "--\nControllo automatico della casella TPL (ogni ora)"
    )

    corpo_html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family: Calibri, 'Segoe UI', Arial, sans-serif; font-size: 11pt; color: #222;">
<p>Buongiorno,</p>
<p>{"ci sono" if quanti > 1 else "c'&egrave;"} <b>{quanti}</b> {plurale} da gestire nella casella della sperimentazione
<b>{html.escape(cfg.imap_user)}</b>.</p>
<p style="margin:14px 0 4px"><b>== Da gestire ({quanti}) ==</b></p>
<ul style="margin:0; padding-left:18px; font-size:10.5pt;">{righe_html}</ul>
<p style="color:#7f8c8d; font-size:9pt; margin-top:18px">--<br>
Controllo automatico della casella TPL (ogni ora)</p>
</body></html>"""

    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((cfg.mittente_nome, cfg.mittente))
    msg["To"] = ", ".join(cfg.destinatari)
    if cfg.copia:
        msg["Cc"] = ", ".join(cfg.copia)
    msg["Subject"] = f"Casella TPL: {quanti} {plurale} da gestire"
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=cfg.mittente.split("@")[-1])
    msg.set_content(testo)
    msg.add_alternative(corpo_html, subtype="html")
    return msg


def invia_email(cfg: Config, messaggi: list) -> bool:
    msg = componi_email(cfg, messaggi)
    destinatari = cfg.destinatari + cfg.copia
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg, from_addr=cfg.mittente, to_addrs=destinatari)
        logger.info(
            "Email di riepilogo inviata",
            extra={"context": {"destinatari": destinatari, "messaggi": len(messaggi)}},
        )
        return True
    except (smtplib.SMTPException, OSError):
        logger.exception("Invio email fallito")
        return False


def invia_telegram(cfg: Config, messaggi: list) -> bool:
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        logger.info("Telegram non configurato: notifica saltata")
        return False

    elenco = ""
    for m in messaggi[:10]:
        elenco += (
            f"\n\n• <b>{html.escape(m['oggetto'])}</b>"
            f"\n  da {html.escape(m['da'])}"
        )
        if m.get("anteprima"):
            elenco += f"\n  <i>{html.escape(m['anteprima'])}</i>"
    coda = f"\n\n(e altri {len(messaggi) - 10})" if len(messaggi) > 10 else ""
    testo = (
        f"\U0001f4e8 <b>Casella TPL Imperia</b>\n"
        f"{len(messaggi)} da gestire{elenco}{coda}"
    )

    inviati = 0
    for chat_id in cfg.telegram_chat_id:
        dati = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": testo, "parse_mode": "HTML"}
        ).encode()
        try:
            richiesta = urllib.request.Request(
                TELEGRAM_API.format(token=cfg.telegram_token), data=dati
            )
            with urllib.request.urlopen(richiesta, timeout=20) as risposta:
                esito = json.loads(risposta.read().decode())
            if esito.get("ok"):
                inviati += 1
            else:
                logger.error(
                    "Telegram ha rifiutato il messaggio",
                    extra={
                        "context": {
                            "chat_id": chat_id,
                            "descrizione": esito.get("description"),
                        }
                    },
                )
        except (urllib.error.URLError, ValueError, OSError):
            logger.exception(
                "Invio Telegram fallito", extra={"context": {"chat_id": chat_id}}
            )

    logger.info(
        "Notifiche Telegram inviate",
        extra={"context": {"riuscite": inviati, "destinatari": len(cfg.telegram_chat_id)}},
    )
    return inviati > 0


def riepilogo_configurazione(cfg: Config) -> dict:
    """Parametri non riservati, per l'interfaccia di gestione.

    Vanno annotati a ogni giro e non solo quando parte una notifica: chi apre
    la pagina deve vedere a chi arriverebbero gli avvisi anche nei giorni in
    cui la casella resta vuota.
    """
    return {
        "casella": cfg.imap_user,
        "destinatari": cfg.destinatari,
        "copia": cfg.copia,
        "telegram": len(cfg.telegram_chat_id),
        "anteprima": cfg.lunghezza_anteprima,
    }


def esegui(cfg: Config, dry_run: bool = False) -> int:
    mancanti = cfg.valida()
    if mancanti:
        logger.error(
            "Configurazione incompleta", extra={"context": {"mancanti": mancanti}}
        )
        return 2

    stato = carica_stato(cfg.state_file)
    ultimo_uid = int(stato.get("ultimo_uid", 0))

    try:
        messaggi, massimo = leggi_nuovi(cfg, ultimo_uid)
    except (imaplib.IMAP4.error, OSError):
        logger.exception("Lettura della casella fallita")
        return 1

    adesso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not messaggi:
        logger.info(
            "Nessun messaggio nuovo", extra={"context": {"ultimo_uid": ultimo_uid}}
        )
        if not dry_run:
            # Anche a mani vuote si annota il passaggio: l'interfaccia di
            # gestione deve poter distinguere "nulla da fare" da "non gira piu'".
            # Se in casella e' arrivato solo traffico da ignorare si sposta anche
            # il cursore, altrimenti quei messaggi verrebbero riesaminati sempre.
            stato["ultimo_controllo"] = adesso
            stato["configurazione"] = riepilogo_configurazione(cfg)
            if massimo > ultimo_uid:
                stato["ultimo_uid"] = massimo
            salva_stato(cfg.state_file, stato)
        return 0

    logger.info(
        "Messaggi nuovi rilevati",
        extra={"context": {"quantita": len(messaggi), "ultimo_uid": ultimo_uid}},
    )

    if dry_run:
        for m in messaggi:
            logger.info(
                "Anteprima",
                extra={
                    "context": {
                        "uid": m["uid"],
                        "da": m["da"],
                        "oggetto": m["oggetto"],
                    }
                },
            )
        return 0

    inviata = invia_email(cfg, messaggi)
    invia_telegram(cfg, messaggi)

    if not inviata:
        # Senza email il cursore resta indietro: al giro successivo si riprova.
        logger.warning("Stato non aggiornato: la notifica non e' partita")
        return 1

    stato.update(
        {
            "ultimo_uid": massimo,
            "ultimo_controllo": adesso,
            "ultima_notifica": adesso,
            "ultimi_notificati": len(messaggi),
            # Riepilogo per l'interfaccia di gestione: mostra cosa e' stato
            # segnalato senza dover riaprire la casella.
            "ultimi_messaggi": [
                {
                    "data": m["data"],
                    "da": m["da"],
                    "oggetto": m["oggetto"],
                    "anteprima": m.get("anteprima", ""),
                }
                for m in messaggi[-10:]
            ],
            "configurazione": riepilogo_configurazione(cfg),
        }
    )
    salva_stato(cfg.state_file, stato)
    return 0


def mostra(cfg: Config) -> int:
    logger.info(
        "Stato corrente",
        extra={"context": carica_stato(cfg.state_file) or {"stato": "assente"}},
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sorveglianza della casella TPL del Comune di Imperia"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Elenca i messaggi nuovi senza notificare e senza salvare lo stato",
    )
    parser.add_argument("--show", action="store_true", help="Mostra lo stato salvato")
    parser.add_argument("--debug", action="store_true", help="Log verboso")
    argomenti = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if argomenti.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = Config()
    if argomenti.show:
        return mostra(cfg)
    return esegui(cfg, dry_run=argomenti.dry_run)


if __name__ == "__main__":
    sys.exit(main())
