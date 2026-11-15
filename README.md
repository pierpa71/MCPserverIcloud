# iCloud Mail MCP

Server MCP che collega Claude alla tua casella **iCloud Mail** (IMAP/SMTP di Apple).
Gira in Docker e si usa come **connettore personalizzato** in Claude, oppure in locale con Claude Desktop / Claude Code.

## Cosa può fare Claude

| Tool | Funzione | Attivo quando |
|---|---|---|
| `list_folders` | Elenco cartelle con totale messaggi e non letti | sempre |
| `search_emails` | Ricerca (testo, mittente, destinatario, oggetto, date, non lette, contrassegnate), dalla più recente | sempre |
| `read_email` | Legge un messaggio: intestazioni, corpo in testo (l'HTML viene convertito), elenco allegati | sempre |
| `get_attachment` | Scarica un allegato: PDF/CSV/TXT/HTML restituiti come testo, immagini come immagini, altri file in base64 | sempre |
| `create_draft` | Crea una bozza in *Bozze* (anche in risposta a un messaggio, con thread e citazione) | se non è `READ_ONLY` |
| `move_email` / `trash_email` | Sposta messaggi / li mette nel Cestino (recuperabili) | se non è `READ_ONLY` |
| `set_flags` | Letto/non letto, contrassegnato | se non è `READ_ONLY` |
| `send_email` | **Invio immediato** via SMTP | solo con `ICLOUD_ALLOW_SEND=true` |

L'impostazione predefinita è prudente: Claude può leggere e preparare **bozze**, che poi rivedi e invii tu da Mail su Mac o iPhone. Non può cancellare definitivamente niente.

---

## 1. Password specifica per app

1. Vai su <https://account.apple.com> → **Accesso e sicurezza** → **Password specifiche per le app**.
2. Generane una (es. "Claude MCP") e copiala: è nel formato `xxxx-xxxx-xxxx-xxxx`.

Ti serve l'autenticazione a due fattori attiva. La password dell'Apple ID non funziona e non va usata. Per revocare l'accesso basta eliminare questa password.

## 2. Configurazione

```bash
cp .env.example .env
openssl rand -hex 32     # usa l'output come MCP_PATH_SECRET
openssl rand -hex 32     # (facoltativo) come MCP_BEARER_TOKEN
nano .env
```

Campi obbligatori: `ICLOUD_EMAIL`, `ICLOUD_APP_PASSWORD` e almeno uno tra `MCP_PATH_SECRET` e `MCP_BEARER_TOKEN`. Senza autenticazione il server in HTTP non parte.

## 3. Avvio

```bash
docker compose up -d --build
docker compose logs -f              # deve comparire "Listening on ..."
curl http://127.0.0.1:8000/health   # {"status":"ok",...}
```

La porta è pubblicata solo su `127.0.0.1`. Per usarlo da claude.ai serve un URL **HTTPS pubblico**, perché i connettori remoti si collegano dai server di Anthropic. Hai due strade.

### Opzione A: Cloudflare Tunnel (consigliata, nessuna porta da aprire)

1. In Cloudflare Zero Trust → *Networks* → *Tunnels*, crea un tunnel e copia il token in `CLOUDFLARE_TUNNEL_TOKEN` nel file `.env`.
2. Nel tunnel aggiungi un *Public hostname*, per esempio `mail-mcp.tuodominio.it`, con servizio `http://icloud-mail-mcp:8000`.
3. Avvia con:
   ```bash
   docker compose --profile tunnel up -d --build
   ```

### Opzione B: reverse proxy esistente

Se usi già Nginx Proxy Manager, Traefik o Caddy (anche quello di Home Assistant), punta un host HTTPS a `http://<host-docker>:8000`. Se il proxy gira su un'altra macchina, in `docker-compose.yml` cambia la porta in `"8000:8000"`.

Esempio Caddy:
```
mail-mcp.tuodominio.it {
    reverse_proxy 127.0.0.1:8000
}
```

## 4. Collegarlo a Claude

### claude.ai / app Claude (connettore personalizzato)

Nelle impostazioni di Claude, nella sezione dei connettori, scegli **Aggiungi connettore personalizzato** e inserisci:

```
https://mail-mcp.tuodominio.it/<MCP_PATH_SECRET>/mcp
```

Lascia vuoti i campi OAuth. In questo caso la protezione è il segreto nel percorso, quindi tratta l'URL come una password. **Non** impostare `MCP_BEARER_TOKEN`, perché claude.ai non può inviare header personalizzati.

### Claude Code

```bash
claude mcp add --transport http icloud-mail \
  https://mail-mcp.tuodominio.it/<MCP_PATH_SECRET>/mcp \
  --header "Authorization: Bearer <MCP_BEARER_TOKEN>"
```

### Claude Desktop in locale (stdio, senza esporre nulla)

Costruisci l'immagine (`docker compose build`) e aggiungi a `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "icloud-mail": {
      "command": "docker",
      "args": ["run", "-i", "--rm",
               "--env-file", "/percorso/assoluto/icloud-mail-mcp/.env",
               "-e", "MCP_TRANSPORT=stdio",
               "icloud-mail-mcp:latest"]
    }
  }
}
```

In modalità stdio il segreto e il token non servono.

---

## Note su iCloud

- **Cartelle standard:** `INBOX`, `Drafts`, `Sent Messages`, `Archive`, `Junk`, `Deleted Messages`. Le cartelle che hai creato tu, anche con lettere accentate, sono supportate.
- **UID:** ogni messaggio ha un UID valido *all'interno della sua cartella*. Claude lo ricava con `search_emails` e lo riusa con `read_email`.
- **Copia degli inviati:** se dopo `send_email` vedi i messaggi duplicati in *Inviati*, imposta `ICLOUD_SAVE_SENT=false`.
- **Alias:** per inviare da un alias iCloud, impostalo in `ICLOUD_FROM_ADDRESS`. L'alias deve essere attivo sul tuo account.

## Sicurezza

- Il container gira come utente non root, con filesystem in sola lettura e senza capability.
- La password resta nel file `.env` sul tuo server. In alternativa puoi usare i Docker secrets con `ICLOUD_APP_PASSWORD_FILE`, `MCP_BEARER_TOKEN_FILE` e `MCP_PATH_SECRET_FILE`.
- Chi conosce l'URL con il segreto può leggere la tua posta. Se pensi che sia trapelato, cambia `MCP_PATH_SECRET`, riavvia e aggiorna il connettore. Per bloccare tutto subito, revoca la password specifica per app.
- Per un uso più prudente: `ICLOUD_READ_ONLY=true`.

## Aggiornare

```bash
docker compose up -d --build
```
