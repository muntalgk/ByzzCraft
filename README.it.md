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

Può anche creare un server da zero: scarica `server.jar` da Mojang e Java da [Eclipse Temurin](https://adoptium.net)
se mancano. Server vanilla, Paper (e derivati) e Fabric li riconosce dal jar; se il tuo ha un nome strano imposta
`server_jar` in `panel-config.json`.

## Installazione

Serve **Python 3.10 o successivo**. Il resto (server Minecraft, Java, texture) lo può scaricare il pannello.

### Su un server senza schermo (Linux, via SSH)

```bash
git clone https://github.com/muntalgk/byzzcraft.git
cd byzzcraft
python3 panel.py
```

Senza git: `curl -L https://github.com/muntalgk/byzzcraft/archive/refs/heads/main.tar.gz | tar xz && cd byzzcraft-main`

Il pannello stampa un link con un codice di configurazione monouso:

```
ByzzCraft is not set up yet.
  From your computer open:  http://192.168.1.64:8765/?setup=Xy3...
```

Aprilo nel browser **del tuo computer**. La pagina chiede la cartella del server (o ne crea uno nuovo lì),
la lingua, il tuo utente admin, chi può aprire il pannello (solo questa macchina o la rete di casa) e se
accetti l'[EULA di Minecraft](https://aka.ms/MinecraftEULA). Senza codice la pagina non si apre, così nessun
altro sulla rete può prendersi l'admin. Se il link non si apre proprio, il firewall blocca la porta:
`sudo ufw allow 8765/tcp`. Link perso? `grep setup= logs/byzzcraft.log`.

Poi fai in modo che resti acceso quando chiudi SSH e dopo un riavvio (il comando stampa un'unit systemd per il
tuo utente e la tua cartella):

```bash
python3 panel.py service | sudo tee /etc/systemd/system/byzzcraft.service
sudo systemctl daemon-reload && sudo systemctl enable --now byzzcraft
```

Preferisci fare tutto da shell? Configura senza browser:

```bash
python3 panel.py setup ~/minecraft --admin alex --lang it --new-server --network --accept-eula
```

(Ci sono anche `--no-java` e `--help`; la password la chiede, oppure la legge da `PANEL_PASSWORD`.)

### Sul tuo PC (Windows o Linux con desktop)

```bash
git clone https://github.com/muntalgk/byzzcraft.git
cd byzzcraft
python panel.py
```

Apri <http://127.0.0.1:8765> sullo stesso PC: la configurazione si apre senza codice. Per avviarlo a ogni
accesso: `python panel.py service` (su Windows lo aggiunge a Esecuzione automatica, senza admin).

### Dopo la configurazione

Il pannello attiva RCON in `server.properties`, copia uno script di avvio nella cartella del server se non
c'è e scarica le texture e i nomi che mostra (sono di Mojang, quindi non stanno nel repository). Se manca
ancora qualcosa (Java, EULA, script di avvio) la **Dashboard** lo mostra con un pulsante per sistemarlo. Premi
**Accendi**.

**Amici**: apri **Account** e aggiungi un utente per ognuno. Gli admin possono usare anche console,
impostazioni, backup e accessi. Per farli entrare in gioco apri la porta di Minecraft: `sudo ufw allow 25565/tcp`
(e inoltrala sul router se giocano da fuori casa).

### Lo script di avvio

Il pannello avvia Minecraft con uno script nella cartella del server: `start.sh` su Linux, `start.bat` su
Windows. La configurazione copia quello di `examples/`. Fa il backup del mondo prima di ogni avvio
(`backups/world_*.zip`) e scrive `logs/gc.log` per il grafico della memoria. Aprilo per cambiare la RAM
(`RAM=4G`). Per usare un tuo script imposta `start_script` in `panel-config.json`; il pannello gli passa
`JAVA` (il suo Java, se ne ha scaricato uno).

## Raggiungerlo da altri dispositivi

"La mia rete di casa" nella configurazione apre il pannello su `http://<ip-del-server>:8765`, senza HTTPS: va
bene in casa, non su internet. Per il resto lascia "solo questa macchina" e scegli uno di questi:

### Tailscale (il più semplice, privato)

Installa [Tailscale](https://tailscale.com) sul server e sui tuoi dispositivi, poi sul server:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8765
```

Il pannello è su `https://<macchina>.<tailnet>.ts.net`, solo per i dispositivi della tua tailnet, con HTTPS.
Gli amici possono entrare nella tua tailnet, oppure puoi [condividere la macchina](https://tailscale.com/kb/1084/sharing).

### nginx (dominio tuo)

Fai puntare un dominio al server, apri le porte 80 e 443, copia `examples/nginx.conf` e prendi un certificato
con certbot: i comandi sono nei commenti del file. Lascia `proxy_buffering off`: serve per gli aggiornamenti dal vivo.

### Caddy (dominio tuo, HTTPS automatico)

Fai puntare un dominio al server, apri le porte 80 e 443 e avvia Caddy con `examples/Caddyfile`. Il
certificato lo prende Caddy da solo.

### Tunnel SSH (nessuna configurazione)

`ssh -L 8765:127.0.0.1:8765 tu@tuo-server`, poi apri <http://127.0.0.1:8765> sul tuo computer.

Con nginx o Caddy il pannello è su internet: usa password lunghe. I login hanno un limite (5 tentativi, poi 15
minuti di blocco). Il proxy deve girare sulla stessa macchina del pannello.

## Log

| File | Cosa contiene |
|---|---|
| `logs/byzzcraft.log` | log tecnico del pannello: avvio, download, errori con traceback (a rotazione, 5 × 1 MB) |
| `logs/server-start.log` | output dello script di avvio, solo l'ultimo: perché Minecraft non è partito |
| `panel.log` | chi ha fatto cosa nel pannello (tab **Registro**) |
| `<server>/logs/latest.log` | il log di Minecraft (tab **Console**) |

Gli admin leggono i primi due anche nel tab **Registro**, senza SSH. Comandi utili:

```bash
journalctl -u byzzcraft -f                          # il pannello, quando gira come servizio
tail -f logs/byzzcraft.log logs/server-start.log    # lo stesso, dai file
sudo systemctl restart byzzcraft                    # riavvia il pannello (Minecraft resta acceso)
python3 panel.py passwd NOME                        # password dimenticata
python3 panel.py java                               # scarica il Java che serve a server.jar
python3 panel.py assets                             # riscarica texture e nomi
```

## Aggiornare

```bash
cd byzzcraft && git pull && sudo systemctl restart byzzcraft
```

Impostazioni, utenti, backup e cache della mappa restano: non stanno nel repository. Quando esce una nuova
release la Dashboard lo dice agli admin, con questo comando; la versione che usi è in fondo alla pagina **Account**.

Backup e ripristini controllano prima lo spazio libero e si fermano con un messaggio invece di riempire il disco;
la Dashboard avvisa quando non ci stanno più tre backup.

## Configurazione

`panel-config.json` lo scrive la configurazione iniziale; puoi modificarlo e riavviare il pannello. Le chiavi
sono descritte nel [README inglese](README.md#configuration).

## Problemi

La Dashboard elenca cosa manca e, quando il server non parte, dice perché. Se non basta,
`logs/server-start.log` ha l'output completo.

- **Il login non resta su `http://192.168…`**: aggiorna il pannello; le versioni vecchie volevano HTTPS per il cookie.
- **"RCON non disponibile"**: riavvia una volta il server Minecraft dopo la configurazione, così legge RCON.
- **Porta 8765 occupata**: c'è già un pannello acceso, oppure cambia `port` in `panel-config.json`.
- **Non avviarlo da root**: i file del server diventerebbero di root. Il pannello lo segnala nel log.

## Licenza

MIT, vedi [LICENSE](LICENSE).

NON È UN PRODOTTO UFFICIALE DI MINECRAFT. NON APPROVATO DA MOJANG O MICROSOFT NÉ ASSOCIATO A ESSI.
