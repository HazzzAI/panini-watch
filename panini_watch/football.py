"""Decide whether a category / product is about (association) football.

Panini's shops sell comics, manga, kids' stickers and US sports too. We only want football.
Terms cover the languages of the official Panini shops.
"""
from __future__ import annotations

import re
import unicodedata


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# Whole-word-ish football terms (compared on accent-stripped lowercase text).
_INCLUDE = [
    r"football", r"soccer", r"futbol", r"futebol", r"fussball", r"voetbal", r"fotboll", r"fotball",
    r"fodbold", r"jalkapallo", r"pilka nozna", r"calcio", r"calciatori", r"labdarugas", r"fotbal",
    r"fifa", r"world[ -]?cup", r"mundial", r"coupe du monde", r"weltmeisterschaft", r"wereldbeker",
    r"copa del mundo", r"copa do mundo", r"coppa del mondo", r"\bwm ?20\d\d", r"\bcm ?20\d\d",
    r"uefa", r"champions", r"europa league", r"conference league", r"euro ?20\d\d", r"\beuro 2028",
    r"premier league", r"\bepl\b", r"laliga", r"la liga", r"\bliga\b", r"megacracks?", r"\beste\b",
    r"liga[- ]este", r"hypermotion", r"bundesliga", r"ligue ?1", r"ligue ?2", r"serie ?a\b", r"eredivisie",
    r"primeira liga", r"liga portugal", r"super ?lig", r"\bmls\b", r"libertadores", r"conmebol",
    r"concacaf", r"copa america", r"copa lib", r"brasileirao", r"campeonato", r"\bdfb\b",
    r"adrenalyn", r"fifa ?365", r"match attax",
    r"panini instant", r"club world cup", r"nations league", r"scudetto", r"fut\b",
    r"ποδοσφαιρο", r"adrenalyn xl", r"liga mx", r"liga profesional", r"liga bbva", r"ekstraklasa",
    r"allsvenskan", r"superligaen", r"eliteserien", r"veikkausliiga", r"jupiler", r"nb ?i\b",
    r"\bsuperliga\b", r"\bpro league\b",
]
# Things that are sports / products we do not want even if a football word slips in.
_EXCLUDE = [
    r"\bnba\b", r"basket", r"baloncesto", r"baseball", r"\bmlb\b", r"\bnfl\b", r"\bnhl\b", r"hockey",
    r"american football", r"\bsports? usa\b", r"us[- ]?sports", r"\bwnba\b", r"ciclismo", r"cycling",
    r"tour de france", r"giro d", r"motogp", r"formula ?1", r"\bf1\b", r"tennis", r"rugby", r"asobal",
    r"handball", r"balonmano", r"volley", r"\bwwe\b", r"\bufc\b", r"euroleague", r"pokemon", r"marvel",
    r"disney", r"dragon ball", r"naruto", r"one piece", r"minecraft", r"manga", r"comic", r"cartoon",
    r"harry potter", r"bluey", r"barbie", r"hello kitty", r"frozen", r"star wars", r"pickleball",
    r"golf\b", r"prizm football",  # US "football" = NFL
    r"justi[cç]a", r"justicia", r"justice", r"giustizia", r"gerechtigkeit", r"sprawiedliwo",   # "Liga da Justica"
    r"batman", r"superman", r"spider", r"avenger", r"vingador", r"x-men", r"\bhulk\b", r"\bthor\b", r"joker",
    r"wonder woman", r"mulher.maravilha", r"deadpool", r"wolverine", r"venom", r"lanterna verde",
    r"green lantern", r"demon slayer", r"jujutsu", r"attack on titan", r"funko", r"lego",
]

_INC = re.compile("|".join(_INCLUDE))
_EXC = re.compile("|".join(_EXCLUDE))


def is_excluded(text: str) -> bool:
    return bool(_EXC.search(_norm(text)))


def is_football(text: str) -> bool:
    """True when the text mentions football and is not obviously another sport/franchise."""
    t = _norm(text)
    return bool(_INC.search(t)) and not _EXC.search(t)


def matches_keywords(text: str, keywords: list[str]) -> bool:
    t = _norm(text)
    return any(_norm(k) in t for k in keywords)
