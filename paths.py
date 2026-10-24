"""Dove stanno i dati di questa istanza: in data/ accanto al codice, oppure nella cartella di $BYZZ_DATA (Docker).
Cosi' il codice si aggiorna o si riclona senza toccare utenti, impostazioni, log, cache e Java scaricato.

Le installazioni di prima della 1.0 tenevano i dati accanto al codice: finche' non c'e' data/ si continua a usarli
li', senza spostare niente (dentro hanno gli stessi nomi: panel-config.json, cache/, logs/, assets/block, ...).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_LEGACY = not os.path.isdir(os.path.join(HERE, "data")) and any(
    os.path.exists(os.path.join(HERE, n)) for n in ("panel-users.json", "panel-config.json"))
DATA = os.path.abspath(os.environ.get("BYZZ_DATA") or (HERE if _LEGACY else os.path.join(HERE, "data")))
ASSETS = os.path.join(DATA, "assets")     # texture e nomi scaricati da Mojang: non si ridistribuiscono
CACHE = os.path.join(DATA, "cache")
LOGS = os.path.join(DATA, "logs")
RUNTIME = os.path.join(DATA, "runtime")   # Java scaricato da Adoptium
