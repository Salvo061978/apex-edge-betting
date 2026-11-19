# APEX EDGE — Guida Avvio Locale + Archivio Unico + Cloudflare Tunnel

Con questa modalità il tuo PC diventa il **server centrale** di APEX EDGE:
- **Unico archivio condiviso** (`data/apex_archive.json`): PC e Smartphone vedono sempre gli stessi numeri (Pre-Match + Live) in tempo reale.
- **Credenziali al sicuro in `.env`** (Telegram + Betfair): mai salvate su GitHub né digitate nel browser.
- **Cloudflare Quick Tunnel gratuito**: a ogni avvio apre un link HTTPS sicuro (`https://xxxx.trycloudflare.com`) e **te lo manda automaticamente su Telegram** per aprirlo sullo smartphone con 1 tap.

---

## 1. Installazione sul PC Windows (1 minuto)
1. Scarica o clona questo repository sul tuo PC:
   ```bat
   git clone https://github.com/Salvo061978/apex-edge-betting.git
   cd apex-edge-betting
   ```
   *(Oppure clicca **Code → Download ZIP** su GitHub ed estrai la cartella sul Desktop.)*
2. Fai doppio click su **`AVVIA_APEX_EDGE.bat`**:
   - La prima volta creerà automaticamente il file **`.env`** e lo aprirà nel Blocco Note.
   - Inserisci almeno `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` (e se vuoi `BETFAIR_APP_KEY`, `BETFAIR_SSOID`, `APEX_ACCESS_PIN`), salva e chiudi il Blocco Note.
   - Se `cloudflared.exe` manca, lo scarica in automatico da Cloudflare.
   - Avvia `server.py` su `http://127.0.0.1:8787` e ti invia il link `https://...trycloudflare.com` su Telegram!

---

## 2. Come unire i tuoi storici esistenti (149 gare PC + 163 Smartphone) senza perdere nulla
1. Apri il vecchio link GitHub Pages sul **PC** → vai su **📈 Storico** → premi **`📤 Esporta Archivio JSON`** (salva il file `apex_backup_...json`).
2. Fai lo stesso sullo **Smartphone** → **📈 Storico** → **`📤 Esporta Archivio JSON`**.
3. Ora apri il nuovo link **Cloudflare** (o `http://127.0.0.1:8787`) → **📈 Storico** → premi **`📥 Importa / Unisci JSON`** e seleziona prima il file del PC e poi quello dello Smartphone.
4. Il server fa il **merge automatico senza duplicati** dentro `data/apex_archive.json`. Da questo momento in poi **PC e Smartphone sono sincronizzati per sempre**!
