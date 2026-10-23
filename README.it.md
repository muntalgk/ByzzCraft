# ByzzCraft

Pannello web self-hosted per un server Minecraft Java vanilla. Un solo file Python da avviare, nessuna
dipendenza, nessun database: legge i file del server e gli parla via RCON.

[Read me in English](README.md)

## Cosa fa

- **Accendi, spegni, riavvia** il server dal browser; si può spegnere da solo quando nessuno gioca da un po'.
- **Dashboard dal vivo**: giocatori online, ora del giorno, fase lunare e meteo, CPU, RAM, tempo per tick, heap Java.
- **Mappa**: vista dall'alto del mondo esplorato (Overworld, Nether, End), posizioni dei giocatori, segnaposti condivisi.
- **Giocatori**: skin 3D, vita, fame, armatura, esperienza, inventario ed ender chest, statistiche.
- **Timeline**: sessioni di gioco, morti, progressi e chat, ricostruiti dai log del server.
- **Console**: log dal vivo e comandi RCON.
- **Impostazioni**: editor di `server.properties` con backup del file a ogni salvataggio.
- **Backup**: backup del mondo manuali e programmati, conservazione, download, ripristino.
- **Accessi**: whitelist, operatori, ban ed espulsioni, anche a server spento.
- **Registro**: chi ha fatto cosa nel pannello.
- Utenti admin o giocatori, interfaccia in inglese e italiano.

## Requisiti

- Python 3.11 o successivo (solo libreria standard).
- Windows o Linux.
- Un server Minecraft Java vanilla (`server.jar`) avviato da uno script.

## Installazione

```bash
git clone https://github.com/<tu>/byzzcraft.git
cd byzzcraft
python panel.py init /percorso/del/server-minecraft
python panel.py assets
python panel.py
```

1. `init` salva `panel-config.json`, attiva RCON in `server.properties` con una password casuale e crea
   il primo admin. Avvia prima il server Minecraft una volta, così `server.properties` esiste.
2. `assets` scarica da Mojang il client della versione del tuo server ed estrae le texture e i nomi in
   inglese che il pannello mostra. Sono protetti da copyright di Mojang, quindi non stanno nel repository.
3. Copia `examples/start.sh` (Linux) o `examples/start.bat` (Windows) nella cartella del server e regola
   RAM e Java. Il pannello conta su quello che fa: backup del mondo prima di ogni avvio e `logs/gc.log`.
4. Apri <http://127.0.0.1:8765> ed entra.
5. Per l'interfaccia in italiano metti `"lang": "it"` in `panel-config.json`.

Altri utenti: `python panel.py adduser NOME [--admin]`, `passwd NOME`, `deluser NOME`, `users`.

Le opzioni di `panel-config.json` sono descritte nel [README inglese](README.md#configuration).

### Avvio automatico

- **Linux**: `examples/byzzcraft.service` (systemd). Avvialo con l'utente proprietario dei file del server, non root.
- **Windows**: un collegamento a `pythonw panel.py` nella cartella Esecuzione automatica (`shell:startup`).

## Sicurezza

Il pannello ascolta solo su `127.0.0.1`. Per raggiungerlo da altri dispositivi mettilo dietro qualcosa che
aggiunga HTTPS e tenga fuori gli estranei, per esempio [Tailscale Serve](https://tailscale.com/kb/1312/serve)
(`tailscale serve --bg --https=443 http://127.0.0.1:8765`) o un reverse proxy come Caddy o nginx.
Non esporre la porta 8765 direttamente su internet.

## Licenza

MIT, vedi [LICENSE](LICENSE).

NON È UN PRODOTTO UFFICIALE DI MINECRAFT. NON APPROVATO DA MOJANG O MICROSOFT NÉ ASSOCIATO A ESSI.
