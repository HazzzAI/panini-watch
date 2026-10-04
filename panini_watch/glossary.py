"""Offline fallback: rough word-level English for trading-card shop names (used only when the online
translators are busy). Whole words, case-insensitive, all shop languages in one table."""
from __future__ import annotations

import re

_WORDS = {
    # Spanish / Portuguese
    "sobres": "packs", "sobre": "pack", "cajita": "box", "caja": "box", "álbum": "album", "cromos": "stickers",
    "cromo": "sticker", "cartas": "cards", "colección": "collection", "coleccion": "collection",
    "oficial": "official", "tapa dura": "hardcover", "tapa blanda": "softcover", "edición": "edition",
    "completa": "complete", "completo": "complete", "lote": "bundle", "paquete": "pack", "temporada": "season",
    "fútbol": "football", "futbol": "football", "estampas": "stickers", "láminas": "stickers", "laminas": "stickers",
    "figuritas": "stickers", "figurinhas": "stickers", "envelopes": "packs", "saquetas": "packs",
    "caderneta": "album", "coleção": "collection", "capa dura": "hardcover", "copa do mundo": "world cup",
    "copa del mundo": "world cup", "mundial": "world cup", "caixa": "box", "futebol": "football",
    # Italian
    "bustine": "packs", "bustina": "pack", "scatola": "box", "figurine": "stickers", "raccolta": "collection",
    "calcio": "football", "ufficiale": "official", "copertina rigida": "hardcover", "mondiale": "world cup",
    "stagione": "season", "mancanti": "missing",
    # French
    "pochettes": "packs", "pochette": "pack", "boîte": "box", "boite": "box", "cartes": "cards",
    "vignettes": "stickers", "autocollants": "stickers", "officiel": "official", "officielle": "official",
    "couverture rigide": "hardcover", "coupe du monde": "world cup", "saison": "season",
    # German / Dutch
    "tüten": "packs", "tüte": "pack", "sammelkarten": "trading cards", "kollektion": "collection",
    "offiziell": "official", "sammelalbum": "album", "karton": "box", "fußball": "football", "fussball": "football",
    "zakjes": "packs", "zakje": "pack", "verzamelkaarten": "trading cards", "doos": "box", "officiële": "official",
    "wereldbeker": "world cup", "seizoen": "season", "saison": "season",
    # Polish / Hungarian / Romanian
    "saszetki": "packs", "saszetka": "pack", "naklejki": "stickers", "karty": "cards", "kolekcja": "collection",
    "oficjalna": "official", "mistrzostwa świata": "world cup", "pudełko": "box",
    "tasak": "packs", "matricák": "stickers", "matrica": "sticker", "kártyák": "cards", "gyűjtemény": "collection",
    "hivatalos": "official", "doboz": "box", "világbajnokság": "world cup", "szezon": "season",
    "pachete": "packs", "abțibilduri": "stickers", "carduri": "cards", "colecție": "collection", "cutie": "box",
    # Nordic
    "pakker": "packs", "paket": "packs", "klistermærker": "stickers", "klistermerker": "stickers",
    "klistermärken": "stickers", "samlekort": "trading cards", "samlarkort": "trading cards", "boks": "box",
    "eske": "box", "låda": "box", "offisiell": "official", "officiell": "official", "officiel": "official",
    "tarrat": "stickers", "keräilykortit": "trading cards", "paketti": "pack", "laatikko": "box",
    # Greek / Hebrew
    "φακελάκια": "packs", "αυτοκόλλητα": "stickers", "άλμπουμ": "album", "κάρτες": "cards",
    "παγκόσμιο κύπελλο": "world cup", "שקיות": "packs", "מדבקות": "stickers", "אלבום": "album",
    "קלפים": "cards", "מונדיאל": "world cup",
}

_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(k) for k in sorted(_WORDS, key=len, reverse=True)) + r")(?![\w])",
                 re.I)


def rough(text: str) -> str:
    """Word-for-word English for known trading-card terms; everything else is left as is."""
    return _RE.sub(lambda m: _WORDS[m.group(0).lower()], text)
