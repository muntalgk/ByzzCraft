"""Translations: the texts in the code are English, i18n/<language>.json maps them (a missing entry stays English)."""
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
