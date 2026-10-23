# ByzzCraft

Pannello web self-hosted per un server Minecraft Java vanilla. Accendi il server dal telefono, guarda chi è
online e dove si trova, controlla l'inventario, fai il backup del mondo. Un solo file Python, nessuna
dipendenza, nessun database: legge i file del server e gli parla via RCON.

[Read me in English](README.md)

## Cosa fa

- **Accendi, spegni, riavvia** il server dal browser; si spegne da solo quando nessuno gioca da un po'.
- **Dashboard dal vivo**: giocatori online, ora del giorno, luna e meteo, CPU, RAM, tempo per tick, heap Java.
- **Mappa**: vista dall'alto del mondo esplorato (Overworld, Nether, End), posizioni dei giocatori, segnaposti condivisi.
- **Giocatori**: skin 3D, vita, fame, armatura, esperienza, inventario ed ender chest, statistiche.
- **Timeline**: sessioni di gioco, morti, progressi e chat, ricostruiti dai log del server.
- **Console**: log dal vivo e comandi RCON.
- **Impostazioni**: editor di `server.properties` che salva una copia del file a ogni modifica.
- **Backup**: backup del mondo manuali e programmati, conservazione, download, ripristino.
- **Accessi**: whitelist, operatori, ban ed espulsioni, anche a server spento.
- **Account**: ogni amico ha il suo login; gli admin vedono tutto, i giocatori possono accendere il server e guardare.
- Interfaccia in inglese e italiano, Windows e Linux.

## Installazione

Serve **Python 3.11 o successivo** e una cartella con un server Minecraft Java (`server.jar`).

```bash
git clone https://github.com/muntalgk/byzzcraft.git
cd byzzcraft
python panel.py
```

Poi apri **<http://127.0.0.1:8765>** in un browser **sulla stessa macchina**. La prima pagina chiede:

- la cartella del server Minecraft,
- la lingua,
- il tuo utente admin e la password,
- se accetti l'[EULA di Minecraft](https://aka.ms/MinecraftEULA) (serve per avviare il server).

Fine. Il pannello attiva RCON in `server.properties`, copia uno script di avvio nella cartella del server se
non c'è, e scarica da Mojang le texture e i nomi che mostra (sono di Mojang, quindi non stanno nel
repository). Premi **Accendi** nella dashboard.

> La pagina di configurazione si apre solo dalla macchina stessa, così nessun altro può prendersi l'admin.
> Su un server senza desktop apri un tunnel SSH e usa il browser del tuo PC:
> `ssh -L 8765:127.0.0.1:8765 tu@tuo-server`, poi apri <http://127.0.0.1:8765>.

**Amici**: apri **Account** nel pannello e aggiungi un utente per ognuno. Gli admin possono usare anche
console, impostazioni, backup e accessi.

### Lo script di avvio

Il pannello avvia Minecraft con uno script nella cartella del server: `start.sh` su Linux, `start.bat` su
Windows. La configurazione copia quello di `examples/` se non ne hai uno. Fa il backup del mondo prima di ogni
avvio (`backups/world_*.zip`) e scrive `logs/gc.log` per il grafico della memoria. Aprilo per cambiare la RAM
(`RAM=4G`) o il percorso di Java. Per usare un tuo script imposta `start_script` in `panel-config.json`.

### Tenerlo sempre acceso

- **Windows**: premi Win+R, scrivi `shell:startup` e crea lì un collegamento a
  `pythonw C:\percorso\byzzcraft\panel.py` (`pythonw` lo avvia senza finestra).
- **Linux**: usa `examples/byzzcraft.service` con systemd, con l'utente proprietario dei file del server, non root.

Il server Minecraft resta acceso se riavvii il pannello.

## Raggiungerlo da altri dispositivi

Il pannello ascolta solo su `127.0.0.1` e non è mai esposto direttamente. Scegli un modo per raggiungerlo:

### Tailscale (il più semplice, privato)

Installa [Tailscale](https://tailscale.com) sul server e sui tuoi dispositivi, poi sul server:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8765
```

Il pannello è su `https://<macchina>.<tailnet>.ts.net`, solo per i dispositivi della tua tailnet, con HTTPS.
Gli amici possono entrare nella tua tailnet, oppure puoi [condividere la macchina](https://tailscale.com/kb/1084/sharing).

### nginx (dominio tuo)

Fai puntare un dominio al server, apri le porte 80 e 443, copia `examples/nginx.conf` e prendi un certificato
con certbot: i comandi sono nei commenti del file. Lascia `proxy_buffering off`: serve per gli aggiornamenti dal vivo.

### Caddy (dominio tuo, HTTPS automatico)

Fai puntare un dominio al server, apri le porte 80 e 443 e avvia Caddy con `examples/Caddyfile`. Il
certificato lo prende Caddy da solo.

Con nginx o Caddy il pannello è su internet: usa password lunghe. I login hanno un limite (5 tentativi, poi 15
minuti di blocco). Il proxy deve girare sulla stessa macchina del pannello.

## Configurazione

`panel-config.json` lo scrive la configurazione iniziale; puoi modificarlo e riavviare il pannello. Le chiavi
sono descritte nel [README inglese](README.md#configuration).

## Problemi

- **Password dimenticata**: sul server, `python panel.py passwd NOME`.
- **Mappa e oggetti senza immagini**: il download da Mojang non è riuscito; esegui `python panel.py assets`.
- **"RCON non disponibile"**: riavvia una volta il server Minecraft dopo la configurazione, così legge RCON.
- **Configurazione su un server remoto**: usa il tunnel SSH descritto sopra.

## Licenza

MIT, vedi [LICENSE](LICENSE).

NON È UN PRODOTTO UFFICIALE DI MINECRAFT. NON APPROVATO DA MOJANG O MICROSOFT NÉ ASSOCIATO A ESSI.
