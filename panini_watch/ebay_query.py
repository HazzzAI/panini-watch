"""Understand a collector's query and re-write it the way sellers write titles on each eBay site.

"Panini 2022 world cup complete album hardcover" becomes concepts:
    year 2022 · World Cup (or "Qatar 2022") · album · complete · hardcover (preferred)
which are then written per language, e.g. ebay.de: "panini wm 2022 album komplett",
ebay.it: "panini mondiali 2022 album completo", and every listing title is checked against the
concepts in *all* languages (a German title on ebay.co.uk still matches).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .football import _norm

LANGS = ("en", "de", "fr", "it", "es", "nl", "pl")


@dataclass(frozen=True)
class Concept:
    key: str
    kind: str                    # tournament | product | attr | edition | brand
    match: str                   # regex on accent-free lowercase titles (all languages)
    q: dict                      # how sellers write it, per language
    level: str = "hard"          # hard = must be in the title, soft = preferred
    neg: str = ""                # titles with this are NOT what the user means


def _c(key, kind, match, q, level="hard", neg=""):
    if isinstance(q, str):
        q = {lang: q for lang in LANGS}
    return Concept(key, kind, match, q, level, neg)


CONCEPTS: list[Concept] = [
    # ---- tournaments / collections ------------------------------------------------------------
    _c("world_cup", "tournament",
       r"world ?cup|\bwc\b|\bwm\b|weltmeisterschaft|mondial|mundial|coupe du monde|copa do mundo|"
       r"\bwk\b|wereldkampioenschap|mistrzostw|campeonato mundial|fifa world",
       {"en": "world cup", "de": "wm", "fr": "coupe du monde", "it": "mondiali", "es": "mundial", "nl": "wk",
        "pl": "mundial"}),
    _c("euro", "tournament", r"\beuro\b|\bem\b|europameisterschaft|europeo|eurocopa|championnat d.?europe|\bek\b",
       {"en": "euro", "de": "em", "fr": "euro", "it": "euro", "es": "eurocopa", "nl": "ek", "pl": "euro"}),
    _c("club_world_cup", "tournament", r"club world cup|klub.?wm|mundial de clubes|coupe du monde des clubs",
       "club world cup"),
    _c("champions_league", "tournament", r"champions ?league|\bucl\b|liga de campeones", "champions league"),
    _c("copa_america", "tournament", r"copa america", "copa america"),
    _c("fifa365", "tournament", r"fifa ?365", "fifa 365"),
    _c("calciatori", "tournament", r"calciatori", "calciatori"),
    _c("adrenalyn", "tournament", r"adrenalyn", "adrenalyn"),
    _c("megacracks", "tournament", r"megacracks?", "megacracks"),
    _c("liga_este", "tournament", r"liga este|\beste\b", "liga este"),
    _c("bundesliga", "tournament", r"bundesliga", "bundesliga"),
    _c("premier_league", "tournament", r"premier league|\bepl\b", "premier league"),
    _c("laliga", "tournament", r"la ?liga", "liga"),
    _c("libertadores", "tournament", r"libertadores", "libertadores"),
    # ---- products -------------------------------------------------------------------------------
    _c("album", "product", r"\balbum|\balbu\b|sammelalbum|caderneta|albumu?\b",
       "album", neg=r"\bleer|leeralbum|\bempty\b|\bvuot[oae]\b|\bvaci[oa]s?\b|\bvide\b|\bleeg\b|\bpust[ya]\b|"
                   r"ohne sticker|sin cromos|senza figurine|sans vignettes|no stickers"),
    _c("box", "product", r"\bbox\b|\bboxes\b|display|karton|scatola|caja|cajita|\bboite|\bdoos\b|pudelko|"
                         r"booster box|sealed box|caixa",
       {"en": "box", "de": "display", "fr": "boite", "it": "box", "es": "caja", "nl": "box", "pl": "box"},
       neg=r"leeres? display|empty box|box vuot|caja vacia|boite vide|\bleer\b"),
    _c("packs", "product", r"packs?|packets?|tuten|tuete|tute|pochettes?|bustin[ea]|sobres?|zakjes|saszet",
       {"en": "packs", "de": "tüten", "fr": "pochettes", "it": "bustine", "es": "sobres", "nl": "zakjes",
        "pl": "saszetki"}),
    _c("stickers", "product", r"stickers?|aufkleber|vignettes?|figurin[ea]|cromos?|naklejk|figuritas?|"
                              r"figurinhas?|estampas?|images",
       {"en": "stickers", "de": "sticker", "fr": "vignettes", "it": "figurine", "es": "cromos", "nl": "stickers",
        "pl": "naklejki"}),
    _c("cards", "product", r"\bcards?\b|karten|cartas|cartes|\bcarte\b|trading card", "cards"),
    _c("tin", "product", r"\btin\b|blechdose|lattina|\bmetal box", "tin"),
    _c("poster", "product", r"poster", "poster"),
    # ---- attributes -------------------------------------------------------------------------------
    _c("complete", "attr",
       r"complet|komplett|vollstandig|vollst|compleet|kompletn|komplet\b|full set|full album|all stickers|"
       r"alle sticker|alle bilder|100 ?%|\blleno|\bpieno|\brempli|filled|completo|completa",
       {"en": "complete", "de": "komplett", "fr": "complet", "it": "completo", "es": "completo", "nl": "compleet",
        "pl": "komplet"},
       neg=r"incomplet|unvollst|nicht komplett|not complete|missing|fehlend|mancant|faltan|manquant|"
           r"\bempty\b|\bleer\b|\bvuoto\b|\bvacio\b|\bvide\b|ohne sticker|no stickers"),
    _c("hardcover", "attr",
       r"hard ?cover|hardback|hard back|gebunden|tapa dura|copertina rigida|rigido|cartonn|capa dura|harde kaft|"
       r"twarda|platinum|gold edition",
       {"en": "hardcover", "de": "hardcover", "fr": "cartonne", "it": "rigida", "es": "tapa dura", "nl": "hardcover",
        "pl": "twarda"}, level="soft"),
    _c("softcover", "attr", r"soft ?cover|softback|brosch|tapa blanda|copertina morbida", "softcover", level="soft"),
    _c("sealed", "attr", r"sealed|\bovp\b|versiegelt|scell|sigillat|precintad|sellad|zafoliow|\bnew\b|\bneu\b|nuovo|nuevo",
       {"en": "sealed", "de": "ovp", "fr": "scelle", "it": "sigillato", "es": "precintado", "nl": "sealed",
        "pl": "zafoliowany"}),
    _c("empty", "attr", r"\bempty\b|\bleer\b|\bvuoto\b|\bvacio\b|\bvide\b|\bleeg\b|\bpusty\b",
       {"en": "empty", "de": "leer", "fr": "vide", "it": "vuoto", "es": "vacio", "nl": "leeg", "pl": "pusty"}),
    # ---- country editions --------------------------------------------------------------------------
    _c("swiss", "edition", r"swiss|schweiz|suisse|svizzer|switzerland|helvet|\bch\b|suiza|zwitser|szwajcar",
       {"en": "swiss", "de": "schweiz", "fr": "suisse", "it": "svizzera", "es": "suiza", "nl": "zwitserland",
        "pl": "szwajcaria"}),
    _c("german", "edition", r"german|deutsch|allemand|tedesc|aleman|\bduits", {"en": "german", "de": "deutsche",
       "fr": "allemande", "it": "tedesca", "es": "alemana", "nl": "duitse", "pl": "niemiecka"}),
    _c("austrian", "edition", r"austria|osterreich|oesterreich|autrich", {"en": "austria", "de": "österreich"}),
    _c("italian", "edition", r"italian|italien|italia|italiana|italiano", {"en": "italian", "de": "italien",
       "fr": "italienne", "it": "italiana", "es": "italiana", "nl": "italiaanse", "pl": "wloska"}),
    _c("spanish", "edition", r"spanish|spanien|espagn|spagnol|espanol|espana|spaanse", {"en": "spanish",
       "de": "spanien", "fr": "espagnole", "it": "spagnola", "es": "española", "nl": "spaanse"}),
    _c("french", "edition", r"french|franzos|francais|francese|frances|franse", {"en": "french", "de": "französisch",
       "fr": "française", "it": "francese", "es": "francesa", "nl": "franse"}),
    _c("uk", "edition", r"\buk\b|english|england|british|englisch|anglais|ingles|inglese", {"en": "uk",
       "de": "england", "fr": "anglaise", "it": "inglese", "es": "inglesa"}),
    _c("us", "edition", r"\busa?\b|american|amerikan|americaine|americana", {"en": "usa", "de": "usa"}),
    _c("dutch", "edition", r"dutch|nederland|holland|niederland|neerlandais|olandese", {"en": "dutch", "nl": "nederlandse",
       "de": "niederlande"}),
    _c("belgian", "edition", r"belgi", {"en": "belgium", "de": "belgien", "fr": "belgique", "nl": "belgie"}),
    _c("portuguese", "edition", r"portug", {"en": "portugal", "de": "portugal", "es": "portugal"}),
    _c("brazil_ed", "edition", r"brazil|brasil", {"en": "brazil", "de": "brasilien", "es": "brasil", "it": "brasile"},
       level="hard"),
    _c("argentina", "edition", r"argentin", {"en": "argentina", "de": "argentinien", "es": "argentina"}),
    _c("mexico", "edition", r"mexic|mejic", {"en": "mexico", "es": "mexico", "de": "mexiko"}),
    _c("greek", "edition", r"greek|greece|griechen|grec|grecia", {"en": "greek", "de": "griechenland", "fr": "grece"}),
    _c("international", "edition", r"international|internazional|internacional", "international"),
    _c("nordic", "edition", r"nordic|scandinav|skandinav|danish|swedish|norwegian|dansk|svensk|norsk", "nordic"),
    _c("polish", "edition", r"polish|polska|polnisch|polonais", {"en": "polish", "pl": "polska", "de": "polnisch"}),
    # ---- brand ---------------------------------------------------------------------------------------
    _c("panini", "brand", r"panini", "panini", level="soft"),
]
BY_KEY = {c.key: c for c in CONCEPTS}

# Host countries: "Brasil 2014" means the 2014 World Cup even without the words "World Cup".
HOSTS = {
    "world_cup": {1970: r"mexic", 1974: r"munchen|munich|deutschland|germany|allemagne", 1978: r"argentin",
                  1982: r"espana|spain|spanien|espagne", 1986: r"mexic", 1990: r"italia", 1994: r"\busa\b|america",
                  1998: r"france|frankreich", 2002: r"korea|japan", 2006: r"deutschland|germany|allemagne",
                  2010: r"south africa|sudafrika|sudafrica|afrique du sud|zuid", 2014: r"brasil|brazil|bresil",
                  2018: r"russia|russland|rusia|russie|rossija", 2022: r"qatar|katar", 2026: r"usa|canada|mexic"},
    "euro": {1980: r"italia", 1984: r"france", 1988: r"deutschland|germany", 1992: r"sweden|schweden",
             1996: r"england", 2000: r"belgi|nederland|holland", 2004: r"portugal", 2008: r"austria|osterreich|schweiz",
             2012: r"poland|polen|ukrain", 2016: r"france|frankreich", 2020: r"euro", 2021: r"euro",
             2024: r"deutschland|germany"},
}
HOST_WORD = {("world_cup", 2022): "qatar", ("world_cup", 2014): "brasil", ("world_cup", 2018): "russia",
             ("world_cup", 2010): "south africa", ("world_cup", 2006): "germany", ("world_cup", 2026): "2026",
             ("euro", 2024): "germany", ("euro", 2008): "austria", ("euro", 2016): "france"}

# Never what a collector means by a product: copies, singles, digital.
_GLOBAL_NEG = r"replica|reproduc|reprint|nachdruck|ristampa|facsimil|fake|\bcopy\b|digital|\bnft\b|\bpdf\b"
# "Complete" that is not really complete: partly filled, a %, or single stickers "to complete" your album.
_PARTIAL = (r"teilweise|partial|partly|parzial|parcial|partiel|\bhalf\b|\bhalb|fast komplett|almost complete|"
            r"quasi complet|casi complet|nearly complete|\b[1-9]\d?\s?%|\bpick\b|choose|you pick|elige|escoge|scegli|"
            r"aussuchen|auswahl|\bwahle|choisi|to complete|complete your|zum vervollst|per completare|para completar|"
            r"pour completer|vervollstandig|\bsingles?\b|einzeln|sciolt|sueltos?|\blose\b|loose")
# Accessories that mention the album: display cases, sleeves, frames.
_ACCESSORY = r"vitrine|deflector|display case|schutzhulle|\bhulle\b|\bsleeves?\b|protector|\bframe\b|bilderrahmen|cornice"
# Other Panini collections that share words with the main one (excluded unless the user names them).
_OTHER = (r"des bleus|road to|club world cup|klub ?wm|club wm|mundial de clubes|women|frauen|femen|femin|"
          r"femmin|damen|adrenalyn|top class|prizm|\btcg\b|blaster|mini ?album|\blegends?\b|world class|"
          r"1970 ?- ?20\d\d|history|historia|storia|geschichte|\bu ?21\b|\bu-21\b")
# Promo sub-sets that are "complete" on their own (only excluded when the user did not ask for them).
_SUBSET = r"coca|cola|\bupdate|upgrade|extra stickers?|erganzung|nachtrag|\blidl\b|kaufland|\brewe\b|edeka|carrefour|\bpromo"
_STOP = {"the", "a", "an", "of", "and", "edition", "version", "official", "original", "for", "with", "in", "by",
         "ed", "vers", "set"}


@dataclass
class Intent:
    text: str
    concepts: list[Concept] = field(default_factory=list)
    years: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)          # leftover words (player names, ...)
    words_tr: dict = field(default_factory=dict)            # word -> {lang: translation}

    def has(self, key: str) -> bool:
        return any(c.key == key for c in self.concepts)

    def summary(self) -> str:
        parts = []
        if self.years:
            parts.append(" / ".join(self.years))
        for kind in ("tournament", "edition", "product", "attr"):
            for c in self.concepts:
                if c.kind == kind:
                    label = c.key.replace("_", " ").replace(" ed", "")
                    parts.append(label + (" (preferred)" if c.level == "soft" else ""))
        parts += [f"“{w}”" for w in self.words]
        return " · ".join(parts)


def parse(query: str) -> Intent:
    t = _norm(query)
    t = re.sub(r"(\d{4})\s*[-/]\s*(\d{2,4})", lambda m: f"{m.group(1)}-{m.group(2)[-2:]}", t)
    intent = Intent(query)
    intent.years = re.findall(r"\b(?:19[5-9]\d|20[0-4]\d)(?:-\d\d)?\b", t)
    rest = re.sub(r"\b(?:19[5-9]\d|20[0-4]\d)(?:-\d\d)?\b", " ", t)
    for c in CONCEPTS:
        m = re.search(c.match, rest)
        if m:
            intent.concepts.append(c)
            rest = rest[:m.start()] + " " + rest[m.end():]
    # "Panini 2014 world cup Brasil": Brasil is the host, not a "Brazilian edition"
    tourn = next((c for c in intent.concepts if c.kind == "tournament"), None)
    if tourn and intent.years:
        host = HOSTS.get(tourn.key, {}).get(int(intent.years[0][:4]))
        if host:
            intent.concepts = [c for c in intent.concepts
                               if not (c.kind == "edition" and re.search(host, _norm(c.q.get("en", c.key))))]
    for w in re.findall(r"[a-z0-9]+", rest):
        if w not in _STOP and len(w) > 1 and not w.isdigit():
            intent.words.append(w)
    return intent


# ---------------------------------------------------------------------------------------------
# queries per language (eBay: space = AND, "(a, b)" = OR, max 100 chars)
# ---------------------------------------------------------------------------------------------
def _term(c: Concept, lang: str) -> str:
    return c.q.get(lang) or c.q.get("en") or c.key


def build_queries(intent: Intent, lang: str) -> list[str]:
    hard = [c for c in intent.concepts if c.level == "hard"]
    soft = [c for c in intent.concepts if c.level == "soft" and c.kind != "brand"]
    year = intent.years[0] if intent.years else ""
    words = [intent.words_tr.get(w, {}).get(lang, w) for w in intent.words]
    tourn = next((c for c in hard if c.kind == "tournament"), None)
    others = [c for c in hard if c is not tourn]

    def q(parts: list[str]) -> str:
        s = " ".join(p for p in parts if p).strip()
        return re.sub(r"\s+", " ", s)[:100]

    base = ["panini", tourn and _term(tourn, lang), year] + [_term(c, lang) for c in others] + words
    queries = [q(base + [_term(c, lang) for c in soft]), q(base)]
    if tourn and year:
        host = HOST_WORD.get((tourn.key, int(year[:4])))
        if host and host != year:
            queries.append(q(["panini", host, year] + [_term(c, lang) for c in others] + words))
    out = []
    for s in queries:
        if s and s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------------------------------------
# does a listing title match what the user wants?
# ---------------------------------------------------------------------------------------------
@dataclass
class Verdict:
    ok: bool
    score: float
    missing_soft: list[str] = field(default_factory=list)
    reason: str = ""
    note: str = ""                                   # e.g. "complete sticker set + empty album"


def judge(intent: Intent, title: str) -> Verdict:
    t = _norm(title)
    t = re.sub(r"(\d{4})\s*[-/]\s*(\d{2,4})", lambda m: f"{m.group(1)}-{m.group(2)[-2:]}", t)
    if re.search(_GLOBAL_NEG, t):
        return Verdict(False, 0, reason="copy / digital")
    asked = _norm(intent.text)
    if (intent.has("complete") or intent.has("album") or intent.has("box")) and re.search(_PARTIAL, t) \
            and not re.search(_PARTIAL, asked):
        return Verdict(False, 0, reason="not really complete / singles")
    if re.search(_ACCESSORY, t) and not re.search(_ACCESSORY, asked):
        return Verdict(False, 0, reason="accessory")
    other = re.search(_OTHER, t)
    if other and not re.search(re.escape(other.group(0)), asked) and not re.search(_OTHER, asked):
        return Verdict(False, 0, reason=f"other collection ({other.group(0)})")
    if re.search(_SUBSET, t) and not re.search(_SUBSET, asked):
        return Verdict(False, 0, reason="promo / update sub-set")
    for y in intent.years:                                   # the season / year must be there
        y4, yy = y[:4], y[2:4]
        short = rf"(wm|wc|euro|em|ek|wk|mondiali|mundial|world cup|qatar|katar|brasil|russia)\s*'?{yy}\b"
        if y not in t and not re.search(rf"\b{y4}\b", t) and not re.search(short, t):
            return Verdict(False, 0, reason=f"not {y}")
    score = 1.0
    missing: list[str] = []
    note = ""
    # "complete set + empty album" = everything needed for a complete album, stickers not stuck in: keep, but say so
    album = BY_KEY["album"]
    if intent.has("album") and intent.has("complete") and not intent.has("empty") and re.search(album.neg, t) \
            and re.search(r"\bset\b|satz|completo|complete set|set complet|kompletter|komplettes|full set|sticker set", t):
        note = "complete sticker set + empty album"
        t = re.sub(album.neg, " ", t)
        t = re.sub(BY_KEY["complete"].neg, " ", t)
    for c in intent.concepts:
        hit = re.search(c.match, t) is not None
        if not hit and c.kind == "tournament" and intent.years:
            host = HOSTS.get(c.key, {}).get(int(intent.years[0][:4]))
            hit = bool(host and re.search(host, t))
        if c.neg and re.search(c.neg, t) and not intent.has("empty"):
            return Verdict(False, 0, reason=f"{c.key}: excluded word")
        if hit:
            score += 1
        elif c.level == "soft":
            if c.kind != "brand":
                missing.append(c.key)
        else:
            return Verdict(False, 0, reason=f"missing {c.key}")
    for w in intent.words:
        alts = {w} | set(intent.words_tr.get(w, {}).values())
        if not any(_norm(a) in t for a in alts):
            return Verdict(False, 0, reason=f"missing {w}")
        score += 1
    # a "complete album" search should not surface single stickers ("sticker nr 123", "#45")
    if (intent.has("album") or intent.has("box")) and re.search(r"(#|\bnr\.?|\bno\.?|\bn\.)\s*\d{1,3}\b", t) \
            and not re.search(r"\d{3,4}\s*(stickers|sticker|figurine|cromos|vignettes|bilder)", t):
        return Verdict(False, 0, reason="single sticker")
    return Verdict(True, score, missing, note=note)
