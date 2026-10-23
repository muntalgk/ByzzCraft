"""Traduzioni: i testi nel codice sono in inglese, i18n/<lingua>.json li mappa (se manca una voce resta l'inglese)."""
import json
import os

LANG = "en"
TABLE = {}


def load(lang):
    global LANG, TABLE
    LANG = lang or "en"
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n", LANG + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            TABLE = json.load(f)
    except OSError:
        TABLE = {}


def t(s):
    return TABLE.get(s, s)
