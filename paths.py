"""Where this instance keeps its data: data/ next to the code, or the folder in $BYZZ_DATA (Docker).
So the code can be updated or cloned again without touching users, settings, logs, cache and downloaded Java.

Installs from before 1.0 kept the data next to the code: as long as there is no data/ it is used there, nothing is
moved (the names inside are the same: panel-config.json, cache/, logs/, assets/block, ...).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_LEGACY = not os.path.isdir(os.path.join(HERE, "data")) and any(
    os.path.exists(os.path.join(HERE, n)) for n in ("panel-users.json", "panel-config.json"))
DATA = os.path.abspath(os.environ.get("BYZZ_DATA") or (HERE if _LEGACY else os.path.join(HERE, "data")))
ASSETS = os.path.join(DATA, "assets")     # textures and names downloaded from Mojang: never redistributed
CACHE = os.path.join(DATA, "cache")
LOGS = os.path.join(DATA, "logs")
RUNTIME = os.path.join(DATA, "runtime")   # Java downloaded from Adoptium
