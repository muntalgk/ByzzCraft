<p align="center"><img src="byzzcraft/static/logo.svg" alt="" width="96"></p>

# ByzzCraft

[![release](https://img.shields.io/github/v/release/muntalgk/ByzzCraft)](https://github.com/muntalgk/ByzzCraft/releases/latest)
[![ci](https://github.com/muntalgk/ByzzCraft/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/muntalgk/ByzzCraft/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)
[![MIT](https://img.shields.io/badge/license-MIT-3fb950)](LICENSE)

Un pannello web per un server Minecraft Java (vanilla, Paper o Fabric) che gira sulla tua macchina. Io lo uso per
accendere il server dal telefono, vedere chi è online e dove, controllare gli inventari e fare i backup del mondo.

È Python puro, senza dipendenze e senza database: legge i file del server e ci parla via RCON. Gira su Windows,
Linux e Docker. [Read in English](README.md).

<p>
  <img src="docs/screenshots/dashboard.jpg" alt="Dashboard" width="49%">
  <img src="docs/screenshots/map.jpg" alt="Mappa" width="49%">
  <img src="docs/screenshots/settings.jpg" alt="Impostazioni" width="75%">
  <img src="docs/screenshots/phone.jpg" alt="La dashboard sul telefono" width="22%">
</p>

## Download

La versione attuale è nella [pagina delle release](https://github.com/muntalgk/ByzzCraft/releases/latest).

- **Windows**: [ByzzCraft-windows.zip](https://github.com/muntalgk/ByzzCraft/releases/latest/download/ByzzCraft-windows.zip).
  Scompattalo e fai doppio clic su `ByzzCraft.bat`.
- **Docker**: `ghcr.io/muntalgk/byzzcraft` (x86 e ARM, quindi va anche su un Raspberry Pi). Vedi [Docker](#docker).
- **Linux**: clona il repository, così per aggiornare basta un `git pull`.

Fuori da Docker serve Python 3.10 o successivo. Server Minecraft, Java e texture li scarica il pannello quando
mancano.

## Cosa fa

- Accende, spegne e riavvia il server; si spegne da solo quando nessuno gioca da un po'
- Dashboard in tempo reale: giocatori, ora del giorno e meteo, CPU, RAM, tick, heap di Java
- Mappa del mondo esplorato con le posizioni dei giocatori in diretta e i segnaposto condivisi
- Pagine dei giocatori: skin 3D, vita, inventario, ender chest, statistiche
- Cronologia di sessioni, morti, progressi e chat, ricostruita dai log del server
- Console con il log in diretta e i comandi RCON
- Editor di `server.properties`, con un backup a ogni salvataggio
- Backup del mondo manuali e programmati, con regole di pulizia, download e ripristino
- Whitelist, operatori, ban e kick, anche a server spento
- Un account per ogni amico: gli admin vedono tutto, i giocatori possono accendere il server e guardarsi intorno

Funziona con Minecraft Java 1.20.5 e successivi, interfaccia in italiano e inglese.

## Installazione

### Server Linux via SSH

```bash
git clone https://github.com/muntalgk/byzzcraft.git
cd byzzcraft
python3 panel.py
```

Stampa un link con un codice usa e getta, tipo `http://192.168.1.20:8765/?setup=Xy3...`. Aprilo dal tuo computer:
la configurazione chiede la cartella del server (o ne crea uno nuovo), il tuo utente admin, chi può aprire il
pannello e se accetti l'[EULA di Minecraft](https://aka.ms/MinecraftEULA). Senza il codice nessun altro sulla rete
può aprire quella pagina. Se il link non si carica, apri la porta (`sudo ufw allow 8765/tcp`, oppure
`sudo firewall-cmd --permanent --add-port=8765/tcp && sudo firewall-cmd --reload`). Perso il link?
`grep setup= data/logs/byzzcraft.log`.

Per tenerlo acceso quando esci e dopo un riavvio:

```bash
python3 panel.py service | sudo tee /etc/systemd/system/byzzcraft.service
sudo systemctl daemon-reload && sudo systemctl enable --now byzzcraft
```

Puoi anche fare la configurazione dalla shell, senza browser:
`python3 panel.py setup ~/minecraft --admin alex --new-server --accept-eula --lang it` (vedi `--help`).

### Windows, o Linux con il desktop

Scompatta lo [zip per Windows](https://github.com/muntalgk/ByzzCraft/releases/latest/download/ByzzCraft-windows.zip)
e fai doppio clic su `ByzzCraft.bat`, oppure clona il repository e lancia `python panel.py`. Il browser si apre
sulla pagina di configurazione. Se manca Python, il `.bat` ti dice come installarlo
(`winget install -e --id Python.Python.3.12`).

`python panel.py service` lo avvia a ogni accesso (su Windows va in Esecuzione automatica, senza admin).

### Docker

```bash
mkdir byzzcraft && cd byzzcraft
curl -fsSLO https://raw.githubusercontent.com/muntalgk/byzzcraft/main/docker-compose.yml
docker compose up -d
docker compose logs
```

Apri il link di configurazione dai log e lascia `/minecraft` come cartella del server. Il file compose usa la rete
dell'host, che su Linux funziona così com'è; su Docker Desktop attivala in Settings > Resources > Network, oppure
usa le righe `ports:` commentate nel file. Su Windows scrivi `curl.exe` invece di `curl`.

Per usare un server che hai già, monta la sua cartella al posto del volume `minecraft` (`- ./minecraft:/minecraft`)
dopo `sudo chown -R 1000:1000 ./minecraft`. Se il container ha un limite di memoria, imposta la RAM in
Impostazioni → Pannello.

### Dopo la configurazione

Il pannello attiva RCON e scarica le texture. Quello che manca ancora (Java, l'EULA) compare nella Dashboard con un
pulsante per sistemarlo. Aggiungi i tuoi amici in Account e apri loro la porta di Minecraft: `sudo ufw allow 25565/tcp`,
oppure su Windows consenti Java quando lo chiede. Inoltrala sul router se giocano da fuori casa.

## Usarlo da fuori casa

L'opzione "rete di casa" della configurazione è HTTP senza cifratura: va bene in casa, non su internet. Altrimenti
lascia "solo questa macchina" e mettici davanti qualcosa:

- **Tailscale**, il più semplice: `sudo tailscale serve --bg --https=443 http://127.0.0.1:8765`, poi
  `https://<macchina>.<tailnet>.ts.net` funziona sui tuoi dispositivi. Puoi [condividere la macchina](https://tailscale.com/kb/1084/sharing) con gli amici.
- **nginx o Caddy** con un tuo dominio: parti da [`examples/nginx.conf`](examples/nginx.conf) o
  [`examples/Caddyfile`](examples/Caddyfile). Il proxy deve girare sulla stessa macchina.
- **Tunnel SSH**: `ssh -L 8765:127.0.0.1:8765 tu@server`, poi <http://127.0.0.1:8765>.

Su internet usa password lunghe. Dopo 5 tentativi sbagliati un indirizzo resta bloccato per 15 minuti.

## Aggiornare

- Linux: `git pull && sudo systemctl restart byzzcraft`
- Windows: scompatta il nuovo zip sopra la vecchia cartella (la tua `data/` non è nello zip, quindi resta), oppure
  `git pull`; poi riavvia il pannello (gira come `pythonw.exe` se parte all'accesso)
- Docker: `docker compose pull && docker compose up -d`

Utenti, impostazioni, backup e cache stanno in `data/` e non vengono toccati. Quando esce una nuova release la
Dashboard lo dice agli admin.

## Configurazione

Quasi tutto è in Impostazioni → Pannello. Il resto sta in `data/panel-config.json` (riavvia il pannello dopo averlo
modificato).

| Chiave | Predefinito | |
|---|---|---|
| `server_dir` | dalla configurazione | cartella del server Minecraft |
| `listen`, `port` | `127.0.0.1`, `8765` | `0.0.0.0` lo apre alla rete di casa |
| `ram` | automatica | es. `6G`; automatica è metà della macchina, da 512 MB a 4 GB |
| `java_args` | | flag Java in più, oltre a quelli di [Aikar](https://docs.papermc.io/paper/aikars-flags) |
| `backup_on_start` | `true` | backup del mondo prima di ogni avvio |
| `auto_stop_minutes` | `30` | spegne dopo tanti minuti senza nessuno, `0` = mai |
| `start_script` | | lancia un tuo script invece di Java (vedi [`examples/`](examples)) |
| `server_jar` | rilevato | per un jar con un nome insolito |
| `mc_address` | | indirizzo mostrato ai giocatori |
| `lang`, `title` | `en`, `ByzzCraft` | |
| `update_check` | `true` | chiede a GitHub una volta al giorno se c'è una nuova release |

## Quando qualcosa non va

La Dashboard dice cosa manca e perché il server non è partito. Se non basta:

- `data/logs/server-start.log`: l'output di Java dell'ultimo avvio
- `data/logs/byzzcraft.log`: il log del pannello (anche nella scheda Registro, o `journalctl -u byzzcraft -f`)
- "RCON non disponibile" subito dopo la configurazione: riavvia una volta il server Minecraft, così legge le impostazioni RCON
- Password dimenticata: `python3 panel.py passwd NOME`
- Non lanciarlo come root, o i file del server diventano di root

## A cosa si collega

Solo per scaricare, mai per mandare dati su di te o sul tuo server: Mojang (jar del server, texture, skin e UUID),
Adoptium (Java, quando manca) e GitHub (una volta al giorno, per le nuove release; `update_check: false` lo spegne).

## Contribuire

Test: `for t in tests/test_*.py; do python3 "$t"; done` (la CI li fa girare su Windows e Linux). Le traduzioni sono
in `byzzcraft/i18n/`; una nuova lingua è un file JSON in più. Leggi [CONTRIBUTING.md](CONTRIBUTING.md) prima di una
pull request e [SECURITY.md](SECURITY.md) per segnalare una vulnerabilità.

## Licenza

MIT. Non è un prodotto ufficiale Minecraft, non è approvato da Mojang o Microsoft né associato a loro.
