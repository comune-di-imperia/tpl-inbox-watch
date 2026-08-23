# tpl-inbox-watch

Sorveglianza della casella di posta della sperimentazione del trasporto pubblico
locale a guida autonoma del **Comune di Imperia**.

L'applicazione della sperimentazione (`app-tpl.comune.imperia.it`) indica ai
cittadini una casella a cui rivolgersi. Questo programma la controlla a
intervalli regolari e, quando arrivano messaggi nuovi, avvisa chi deve
rispondere: una email di riepilogo e, se configurata, una notifica Telegram.

## Come funziona

A ogni giro il programma:

1. si collega alla casella in sola lettura e chiede l'elenco degli UID;
2. considera solo gli UID superiori a quello dell'ultima notifica;
3. scarta i mittenti elencati fra quelli da ignorare (notifiche di servizio);
4. se resta qualcosa, invia il riepilogo e aggiorna lo stato.

Il confronto avviene per **UID** e non per data: la data di un messaggio è
quella dichiarata dal mittente e non riflette necessariamente l'ordine di
arrivo. Lo stato viene aggiornato **solo se la email parte**: se la notifica
fallisce, al giro successivo si riprova invece di perdere il messaggio.

## Requisiti

Python 3.9 o successivo. Nessuna libreria esterna: si usa la sola libreria
standard (`imaplib`, `smtplib`, `email`, `urllib`).

## Configurazione

Tutti i parametri arrivano da variabili d'ambiente; nel codice non è scritta
alcuna credenziale. Copiare `env.example`, compilarlo e proteggerlo:

```bash
cp env.example ~/.config/tpl-inbox-watch/env
chmod 600 ~/.config/tpl-inbox-watch/env
```

| Variabile | Significato |
|---|---|
| `TPL_IMAP_HOST`, `TPL_IMAP_PORT` | server IMAP della casella (IMAPS) |
| `TPL_IMAP_USER`, `TPL_IMAP_PASSWORD` | credenziali della casella sorvegliata |
| `TPL_IMAP_FOLDER` | cartella da controllare (predefinita `INBOX`) |
| `TPL_SMTP_HOST`, `TPL_SMTP_PORT` | server di invio (STARTTLS) |
| `TPL_SMTP_USER`, `TPL_SMTP_PASSWORD` | se vuoti si riusano le credenziali IMAP |
| `TPL_EMAIL_FROM`, `TPL_EMAIL_FROM_NAME` | mittente della notifica |
| `TPL_EMAIL_TO`, `TPL_EMAIL_CC` | destinatari, separati da virgola |
| `TPL_TELEGRAM_TOKEN`, `TPL_TELEGRAM_CHAT_ID` | notifica Telegram (facoltativa) |
| `TPL_IGNORA_MITTENTI` | frammenti di indirizzo da non segnalare |
| `TPL_LUNGHEZZA_ANTEPRIMA` | caratteri di anteprima del testo (`0` la disattiva) |
| `TPL_DESTINATARI_FILE` | elenco destinatari gestito da interfaccia esterna (ha la precedenza) |
| `TPL_STATE_FILE` | file di stato con l'ultimo UID notificato |

## Uso

```bash
python3 main.py             # controlla e notifica
python3 main.py --dry-run   # elenca i messaggi nuovi senza inviare nulla
python3 main.py --show      # mostra lo stato salvato
```

Il programma non richiede privilegi di amministratore.

## Esecuzione periodica

Lo script `tpl-inbox-watch.sh` carica la configurazione, impedisce
sovrapposizioni fra esecuzioni e scrive un registro. Per un controllo orario è
sufficiente una riga nella *crontab* dell'utente:

```
17 * * * * /percorso/tpl-inbox-watch/tpl-inbox-watch.sh
```

## Riservatezza

La casella viene aperta **in sola lettura** e i messaggi sono prelevati con
`BODY.PEEK`: non vengono segnati come letti, spostati o cancellati, e chi apre
la casella li trova esattamente come li avrebbe trovati.

La notifica riporta mittente, oggetto, data e un'**anteprima delle prime
lettere del testo** (256 caratteri di norma), utile per capire se un messaggio
richiede risposta immediata senza aprire la casella. L'anteprima si ricava
dalla sola parte testuale: gli allegati non vengono aperti e le righe di
citazione della conversazione precedente sono scartate.

Se si preferisce non far uscire alcun contenuto dalla casella basta impostare
`TPL_LUNGHEZZA_ANTEPRIMA=0`: in quel caso il corpo dei messaggi non viene
nemmeno scaricato e la notifica torna ai soli mittente, oggetto e data.

## Licenza

Distribuito con licenza **EUPL-1.2** (European Union Public Licence), come
previsto dall'articolo 69 del Codice dell'amministrazione digitale per il
software sviluppato su commessa di una pubblica amministrazione. Vedi
[`LICENSE`](LICENSE).

## Destinatari gestiti dall'esterno

Chi risponde ai cittadini cambia nel tempo. Oltre alle variabili d'ambiente,
il programma accetta un elenco in formato JSON che ha la precedenza su di esse
e viene riletto a ogni giro: così un'interfaccia di gestione può modificarlo
senza che nessuno debba entrare sul server.

```json
{
  "email": [
    {"indirizzo": "nome.cognome@comune.esempio.it", "ruolo": "a", "nota": "ufficio mobilità"},
    {"indirizzo": "altro@comune.esempio.it", "ruolo": "cc", "nota": ""}
  ],
  "telegram": [
    {"chat_id": "123456789", "nome": "Nome Cognome"}
  ]
}
```

Il file contiene **solo** indirizzi e identificativi di chat: nessuna
credenziale, quindi può stare in una cartella condivisa fra l'utenza che
esegue la sorveglianza e quella che esegue l'interfaccia. Se manca, è
illeggibile o non contiene destinatari principali, si torna alle variabili
d'ambiente: restare senza destinatari significherebbe non avvisare nessuno.
