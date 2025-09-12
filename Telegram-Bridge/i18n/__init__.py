# -*- coding: utf-8 -*-
from importlib import import_module
from functools import lru_cache
import config

@lru_cache(maxsize=1)
def _load_strings():
    lang = getattr(config, "LANG", "es").split("-")[0].lower()
    try:
        mod = import_module(f"i18n.strings_{lang}")
    except Exception:
        mod = import_module("i18n.strings_en")
    return getattr(mod, "STRINGS", {})

def t(key: str, **kwargs) -> str:
    strings = _load_strings()
    s = strings.get(key)
    if s is None:
        # fallback a EN si falta la clave
        s = import_module("i18n.strings_en").STRINGS.get(key, key)
    try:
        return s.format(**kwargs)
    except Exception:
        return s
