"""Mask what identifies the household before a document's text leaves the house.

Used by the `private_cloud` reading mode (an option next to local only / local
first / cloud): the cloud model (OpenRouter) reads the document, but only its
**text**, and only after every private detail was replaced by a tag such as
[PERSONNE_1]. The file and the images never leave.

Three layers find what to hide:

1. **known**  what the dashboard already knows: the members' names and birth
   dates, the accounts' IBANs, and the household's own list
   (`settings.privacy.always_mask`: surname, street, AVS n°…).
2. **rules**  IBAN, AVS number, e-mail, phone, card number, street address,
   case postale, the number after « n° client / police / contrat… », the date
   after « né le / date de naissance ».
3. **local model**  the local LLM only *lists* the private bits it sees. It
   never rewrites the text, so it cannot change an amount. An item is kept
   only when it really is in the text; amounts and plain dates are refused.

The code does the replacing (accents, case and spacing ignored; long items are
also found when split by a line break or a hyphen). `verify()` then scans the
masked text again: if a masked item or a rule match is still there, nothing is
sent. `restore()` puts the real values back into the cloud's answer, here.
"""
from __future__ import annotations

import re
import time
import unicodedata

from .. import llm
from . import db

# kind → tag prefix (tags are plain ASCII: the cloud model copies them as is)
TAGS = {
    "person": "PERSONNE", "address": "ADRESSE", "id": "NUMERO", "iban": "IBAN", "avs": "AVS", "email": "EMAIL",
    "phone": "TEL", "contact": "CONTACT", "birthdate": "NAISSANCE", "employer": "EMPLOYEUR", "card": "CARTE",
    "other": "PRIVE",
}
KIND_LABELS = {
    "person": "personne", "address": "adresse", "id": "numéro", "iban": "IBAN", "avs": "n° AVS", "email": "e-mail",
    "phone": "téléphone", "contact": "contact", "birthdate": "date de naissance", "employer": "employeur",
    "card": "carte", "other": "privé",
}
SOURCE_LABELS = {"known": "connu du foyer", "list": "votre liste", "rule": "règle", "local": "IA locale",
                 "manual": "ajouté à la main"}
# kinds made of letters and digits whose separators vary (CH93 0076… / CH930076…, 756.1234… / 7561234…)
FLEX_KINDS = {"iban", "avs", "id", "phone", "card", "birthdate"}
TAG_RE = re.compile(r"\[(%s)_(\d+)\]" % "|".join(TAGS.values()))
LOOSE_TAG_RE = re.compile(r"\[?\b(%s)_(\d+)\b\]?" % "|".join(TAGS.values()))

SQUEEZE_MIN = 8      # items with this many letters/digits are also found across line breaks and hyphens
CHUNK = 3000         # characters of text per local-model call (small models have small contexts)
MAX_CHUNKS = 30
MAX_ITEM = 120       # a local "item" longer than this is a whole line, not a name

# --------------------------------------------------------------------------- text folding


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def _fold_char(ch: str) -> str:
    base = unicodedata.normalize("NFD", ch)[0]
    low = base.lower()
    return low if len(low) == 1 else base


def fold(text: str) -> str:
    """Lower case, no accents, SAME LENGTH as the (NFC) input: positions map one to one."""
    return "".join(_fold_char(c) for c in text)


def _key(text: str) -> str:
    """Identity of an item: letters and digits only, folded."""
    return "".join(c for c in fold(nfc(text)) if c.isalnum())


def _squeeze(folded: str) -> tuple[str, list[int]]:
    """Letters and digits only, with the position of each in `folded`. '#' (a masked tag) is kept as a wall."""
    chars, idx = [], []
    for i, c in enumerate(folded):
        if c.isalnum() or c == "#":
            chars.append(c)
            idx.append(i)
    return "".join(chars), idx


# --------------------------------------------------------------------------- rules

_PARTICLES = r"(?:(?:de|du|des|la|le|les|l['’]|d['’]|della|dei|del|di)\s*)*"
_CAP_WORD = r"[A-ZÀ-ÝÄÖÜ][\wÀ-ÿ'’\-]*"
RULES: list[tuple[str, re.Pattern, int]] = [
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ \u00a0]?[A-Z0-9]{4}){3,7}(?:[ \u00a0]?[A-Z0-9]{1,3})?\b"), 0),
    ("avs", re.compile(r"\b756[.\s]?\d{4}[.\s]?\d{4}[.\s]?\d{2}\b"), 0),
    ("email", re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+"), 0),
    ("phone", re.compile(r"(?<![\w+])(?:\+|00)41[\s.]?(?:\(0\)[\s.]?)?\d{2}(?:[\s.]?\d){7}\b"
                         r"|\b0[1-9]\d(?:[\s.]?\d){7}\b"), 0),
    ("card", re.compile(r"\b(?:\d{4}[ \-]){3}\d{4}\b"), 0),
    ("address", re.compile(
        r"(?i:\b(?:rue|ruelle|chemin|ch|avenue|av|route|rte|place|pl|boulevard|bd|quai|impasse|imp|allée|sentier"
        r"|passage|promenade|esplanade|chaussée|via|viale|piazza|vicolo)\.?)\s+"
        + _PARTICLES + _CAP_WORD + r"(?:\s+" + _PARTICLES + _CAP_WORD + r"){0,3}(?:\s+\d{1,4}\s?[a-zA-Z]?\b)?"), 0),
    ("address", re.compile(r"\b[A-ZÄÖÜ][\wäöüß\-]*(?:strasse|straße|str\.|gasse|weg|platz|allee)"
                           r"\s+\d{1,4}\s?[a-zA-Z]?\b"), 0),
    ("address", re.compile(r"(?i:\b(?:case postale|postfach|casella postale)\s+\d{1,6}\b)"), 0),
    ("id", re.compile(
        r"(?i:(?:n°|no\.?|nr\.?|num[ée]ro|nummer)\s*(?:de\s+|d['’]\s*|du\s+)?(?:client|cliente|police|contrat|"
        r"assur[ée]e?|personnel|contribuable|dossier|membre|employ[ée]e?|soci[ée]taire|adh[ée]rent|affili[ée]e?|"
        r"compte|portefeuille|d[ée]p[ôo]t|r[ée]f[ée]rence)"
        r"|(?:kunden|police|policen|vertrags|versicherten|personal|mitglied|konto|depot|referenz|steuer)"
        r"-?(?:nummer|nr\.?)"
        r"|(?:client|contract|policy|customer|account|member|employee)\s*(?:n°|no\.?|number|id))"
        r"\s*[:#]?\s*([A-Z0-9][A-Z0-9.\-/]*\d[A-Z0-9.\-/]*(?:\s\d{2,}){0,3})"), 1),
    ("birthdate", re.compile(
        r"(?i:\b(?:né\(e\)|née|né|date de naissance|geburtsdatum|geboren(?:\s+am)?|data di nascita|nato|nata|"
        r"date of birth|born)\s*(?:le\s*)?:?)\s*(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})"), 1),
]

AMOUNT_RE = re.compile(r"^[-+−]?\s*(?:CHF|EUR|USD|Fr\.?)?\s*[-+−]?\d[\d'’ \u00a0\u202f.,]*\s*(?:CHF|EUR|USD|Fr\.?|%)?$",
                       re.I)
DATE_RE = re.compile(r"^\d{1,4}[./\-]\d{1,2}[./\-]\d{1,4}$|^\d{1,2}\.?\s+\w+\s+\d{2,4}$|^\d{4}-\d{2}$")
STOP = {"chf", "eur", "usd", "fr", "monsieur", "madame", "mme", "mr", "herr", "frau", "signor", "signora", "total",
        "salaire", "lohn", "suisse", "schweiz", "svizzera", "null", "none", "n/a"}


# « , 1003 Lausanne » or « 1020 Renens » on the next line, right after a street: part of the address
CITY_AFTER = re.compile(r",?[ \t]*(?:\n[ \t]*)?(?:CH-)?[1-9]\d{3}[ \t]+"
                        r"[A-ZÀ-Ý][\wÀ-ÿ'’\-]*(?:[ \-][A-ZÀ-Ý][\wÀ-ÿ'’\-]*)?")


def rule_items(text: str) -> list[dict]:
    found = []  # (start, end, kind)
    for kind, rx, group in RULES:
        for m in rx.finditer(text):
            s, e = m.span(group)
            if kind == "address":
                city = CITY_AFTER.match(text, e)
                e = city.end() if city else e
            t = text[s:e].strip()
            if len(_key(t)) >= 3:
                found.append((s, e, kind))
    # a match inside a bigger one (a « card » inside an IBAN) is not a second item
    inside = {f for f in found for g in found
              if g != f and g[0] <= f[0] and f[1] <= g[1] and g[1] - g[0] > f[1] - f[0]}
    return [{"text": " ".join(text[s:e].split()), "kind": kind, "source": "rule"}
            for s, e, kind in found if (s, e, kind) not in inside]


# --------------------------------------------------------------------------- known secrets


def _date_variants(iso: str) -> list[str]:
    try:
        y, m, d = (int(x) for x in iso[:10].split("-"))
    except ValueError:
        return []
    return [f"{d:02d}.{m:02d}.{y}", f"{d}.{m}.{y}", f"{y}-{m:02d}-{d:02d}", f"{d:02d}.{m:02d}.{y % 100:02d}"]


def guess_kind(text: str) -> str:
    t = text.strip()
    if "@" in t:
        return "email"
    if RULES[0][1].fullmatch(t):
        return "iban"
    if RULES[1][1].fullmatch(t):
        return "avs"
    alnum = [c for c in t if c.isalnum()]
    if alnum and sum(c.isdigit() for c in alnum) >= max(4, 0.6 * len(alnum)):
        return "id"
    return "other"


def known_items(settings: dict | None = None) -> list[dict]:
    """The household's names, birth dates and IBANs (from the finance DB) + its own « always mask » list."""
    out: list[dict] = []
    with db.get_conn() as conn:
        for r in conn.execute("SELECT name, birth_date FROM members"):
            name = (r["name"] or "").strip()
            if name:
                out.append({"text": name, "kind": "person", "source": "known"})
                parts = name.split()
                if len(parts) > 1:
                    out += [{"text": p, "kind": "person", "source": "known"} for p in parts if len(_key(p)) >= 3]
            for v in _date_variants(r["birth_date"] or ""):
                out.append({"text": v, "kind": "birthdate", "source": "known"})
        for r in conn.execute("SELECT iban FROM accounts WHERE iban != ''"):
            out.append({"text": r["iban"], "kind": "iban", "source": "known"})
    for t in ((settings or {}).get("privacy") or {}).get("always_mask") or []:
        if isinstance(t, str) and t.strip():
            out.append({"text": t.strip(), "kind": guess_kind(t), "source": "list"})
    return out


# --------------------------------------------------------------------------- local model

LOCAL_PROMPT = """Tu protèges la vie privée d'un ménage suisse. Voici un extrait d'un document financier.
Liste les données personnelles qui s'y trouvent, recopiées EXACTEMENT comme écrites (mêmes lettres, mêmes espaces) :
- person : nom ou prénom d'une personne (titulaire, salarié·e, conjoint·e, enfant, bénéficiaire, personne citée dans
  une opération)
- address : rue et numéro, case postale
- id : numéro qui identifie une personne ou un contrat (n° AVS, client, police, assuré, contribuable, contrat, compte,
  carte, IBAN)
- contact : e-mail ou téléphone
- birthdate : date de naissance
- employer : nom de l'employeur
Ne liste PAS : les montants, les autres dates, les banques, assurances, caisses de pension, administrations, commerces.
Réponds uniquement en JSON : {"items": [{"text": "...", "kind": "person"}]} — {"items": []} s'il n'y a rien.

Extrait :
\"\"\"
%s
\"\"\""""

_KIND_SYNONYMS = {
    "name": "person", "nom": "person", "personne": "person", "people": "person", "persons": "person",
    "adresse": "address", "street": "address", "numero": "id", "number": "id", "identifier": "id", "account": "id",
    "email": "contact", "e-mail": "contact", "phone": "contact", "telephone": "contact", "téléphone": "contact",
    "date_of_birth": "birthdate", "dob": "birthdate", "naissance": "birthdate", "employeur": "employer",
    "company": "employer",
}


TITLE_RE = re.compile(r"^(?:madame|monsieur|mme\.?|mlle\.?|m\.|mr\.?|mrs\.?|ms\.?|herr|frau|signora|signor|sig\.(?:ra)?)"
                      r"\s+", re.I)


def _chunks(text: str) -> list[str]:
    out, cur, size = [], [], 0
    for line in text.splitlines(keepends=True):
        while len(line) > CHUNK:
            out.append(line[:CHUNK])
            line = line[CHUNK:]
        if cur and size + len(line) > CHUNK:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        out.append("".join(cur))
    return [c for c in out if c.strip()]


def _candidates(data) -> list[tuple[str, str]]:
    """The model's answer → [(text, kind)], whatever shape it chose."""
    if not isinstance(data, dict):
        return []
    raw = data.get("items")
    if raw is None:  # {"person": [...], "address": [...]}
        raw = [{"text": t, "kind": k} for k, v in data.items() if isinstance(v, list) for t in v]
    out = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, str):
            out.append((it, "other"))
        elif isinstance(it, dict) and isinstance(it.get("text"), str):
            kind = str(it.get("kind") or "other").strip().lower()
            out.append((it["text"], kind if kind in TAGS else _KIND_SYNONYMS.get(kind, "other")))
    return out


def _acceptable(text: str, kind: str) -> bool:
    t = text.strip()
    if not t or len(t) > MAX_ITEM or "[" in t or len(_key(t)) < 3 or fold(t) in STOP:
        return False
    if DATE_RE.match(t):
        return kind == "birthdate"
    if AMOUNT_RE.match(t):
        if kind not in ("id", "iban", "avs", "contact", "phone", "card"):
            return False  # a name is not a number
        looks_amount = (re.search(r"[.,]\d{1,2}$", t) or re.search(r"\d['’\u202f]\d{3}", t)
                        or re.search(r"chf|eur|usd|fr|%", t, re.I) or sum(c.isdigit() for c in t) <= 4)
        return not looks_amount
    return True


def detect_local(text: str, settings: dict | None) -> tuple[list[dict], dict]:
    """Ask the local model, chunk by chunk, which private bits it sees. Returns (items, info)."""
    chunks = _chunks(text)
    info: dict = {"engine": None, "chunks": len(chunks), "ok": 0, "errors": [], "attempts": [], "seconds": 0.0,
                  "refused": 0}
    items: list[dict] = []
    folded = fold(text)
    squeezed = _squeeze(folded)
    t0 = time.monotonic()
    fails = 0
    for i, chunk in enumerate(chunks[:MAX_CHUNKS]):
        trace: dict = {}
        try:
            data, engine = llm.chat_json(llm.text_message(LOCAL_PROMPT % chunk), settings, trace=trace,
                                         providers=["local"])
            info["engine"] = engine
            info["ok"] += 1
            fails = 0
            for t, kind in _candidates(data):
                if kind == "person":  # « Madame Léa Dupont »: the title is not private, the name is
                    t = TITLE_RE.sub("", t.strip())
                item = {"text": t.strip(), "kind": kind, "source": "local"}
                if _acceptable(t, kind) and _find(folded, squeezed, item):  # made up by the model: refused
                    items.append(item)
                else:
                    info["refused"] += 1
        except llm.LLMError as e:
            info["errors"].append(f"extrait {i + 1} : {e}"[:300])
            fails += 1
        info["attempts"] += [{**a, "engine": f"{a.get('engine', '')} · extrait {i + 1}/{len(chunks)}"}
                             for a in trace.get("attempts", [])]
        if fails >= 2 and info["ok"] == 0:  # the server is down or the model can't do it: don't wait 30 times
            left = len(chunks) - i - 1
            if left:
                info["errors"].append(f"{left} extrait(s) pas envoyés à l'IA locale")
            break
    if len(chunks) > MAX_CHUNKS:
        info["errors"].append(f"{len(chunks) - MAX_CHUNKS} extrait(s) au-delà de la limite")
    info["seconds"] = round(time.monotonic() - t0, 1)
    return items, info


# --------------------------------------------------------------------------- find, mask, verify


def _flex(item: dict) -> bool:
    return item["kind"] in FLEX_KINDS or (item["kind"] == "other" and guess_kind(item["text"]) != "other")


def _pattern(item: dict) -> re.Pattern | None:
    t = fold(nfc(item["text"])).strip()
    if _flex(item):
        chars = [c for c in t if c.isalnum()]
        if len(chars) < 3:
            return None
        body = r"[\s.\-/'’]{0,2}".join(re.escape(c) for c in chars)
        if t.startswith("+"):  # +41 21 …: the plus belongs to the number
            body = r"\+\s?" + body
            return re.compile(rf"(?<![\w+]){body}(?!\w)")
    else:
        words = t.split()
        if not words:
            return None
        body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?!\w)")


def _find(folded: str, squeezed: tuple[str, list[int]], item: dict) -> list[tuple[int, int]]:
    """Spans of `item` in the text: as written (spacing free), and for long items letters/digits only."""
    spans = []
    pat = _pattern(item)
    if pat:
        spans += [(m.start(), m.end()) for m in pat.finditer(folded)]
    key = _key(item["text"])
    if len(key) >= SQUEEZE_MIN:
        sq, idx = squeezed
        start = sq.find(key)
        while start != -1:
            spans.append((idx[start], idx[start + len(key) - 1] + 1))
            start = sq.find(key, start + 1)
    return _merge(spans)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s, e in sorted(spans):
        if out and s < out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _assign_tags(items: list[dict]) -> None:
    """Give a tag to the items without one, after the highest of their kind."""
    top: dict[str, int] = {}
    for it in items:
        m = TAG_RE.fullmatch(it.get("tag") or "")
        if m:
            top[m.group(1)] = max(top.get(m.group(1), 0), int(m.group(2)))
    for it in items:
        if not it.get("tag"):
            prefix = TAGS.get(it["kind"], "PRIVE")
            top[prefix] = top.get(prefix, 0) + 1
            it["tag"] = f"[{prefix}_{top[prefix]}]"


def mask(text: str, items: list[dict]) -> str:
    """Replace every occurrence of the items that are on. Overlapping finds become one tag (the longest item's),
    so no character of a private item stays visible. Sets `count` and `found` (as written here) on each item."""
    folded = fold(text)
    squeezed = _squeeze(folded)
    spans = []
    for i, it in enumerate(items):
        found = _find(folded, squeezed, it)
        it["count"] = len(found)  # also for an item turned off: the screen says where it is
        it["at"] = found[0][0] if found else len(text) + 1
        if it.get("on", True):
            spans += [(s, e, i) for s, e in found]
    spans.sort()
    groups: list[list[int]] = []  # [start, end, item of the longest span]
    for s, e, i in spans:
        if groups and s < groups[-1][1]:
            g = groups[-1]
            if e - s > g[3]:
                g[2], g[3] = i, e - s
            g[1] = max(g[1], e)
        else:
            groups.append([s, e, i, e - s])
    out, pos = [], 0
    for s, e, i, _ in groups:
        it = items[i]
        it.setdefault("found", text[s:e])
        out += [text[pos:s], it["tag"]]
        pos = e
    out.append(text[pos:])
    return "".join(out)


def verify(masked: str, items: list[dict]) -> dict:
    """Scan the text that would leave once more: no item that is on, and no rule match the household did not
    allow (an item it turned off), may still be in it. {ok, leftovers: [{text, kind, why}]}"""
    walled = TAG_RE.sub(" # ", masked)
    folded = fold(walled)
    squeezed = _squeeze(folded)
    leftovers = []
    for it in items:
        if it.get("on", True) and _find(folded, squeezed, it):
            leftovers.append({"text": it["text"], "kind": it["kind"], "why": "encore présent après masquage"})
    allowed = {_key(it["text"]) for it in items if not it.get("on", True)}
    seen = set()
    for it in rule_items(walled):
        k = _key(it["text"])
        if k not in allowed and k not in seen:
            seen.add(k)
            leftovers.append({"text": it["text"], "kind": it["kind"], "why": "repéré par une règle"})
    return {"ok": not leftovers, "leftovers": leftovers}


def _dedupe(items: list[dict]) -> list[dict]:
    out, seen = [], {}
    for it in items:
        k = _key(it["text"])
        if not k:
            continue
        if k in seen:
            prev = seen[k]
            # a rule or the household knows better than the local model what this is
            if prev["source"] == "local" and it["source"] != "local":
                prev.update(kind=it["kind"], source=it["source"])
            continue
        seen[k] = it
        out.append(it)
    return out


def _build(text: str, items: list[dict]) -> dict:
    masked = mask(text, items)
    # an item that is nowhere in this document is not worth showing (unless the household typed it)
    items = sorted((it for it in items if it.get("count") or it["source"] == "manual"), key=lambda it: it["at"])
    return {"items": items, "masked": masked, "check": verify(masked, items)}


def prepare(text: str, settings: dict | None, source: str) -> dict:
    """Original text → everything the « before sending » screen needs (saved as documents.privacy)."""
    text = nfc(text)
    local, info = detect_local(text, settings)
    items = _dedupe(known_items(settings) + rule_items(text) + local)
    for it in items:
        it["on"] = True
    # number the tags in reading order
    folded = fold(text)
    squeezed = _squeeze(folded)
    first = {id(it): (_find(folded, squeezed, it) or [(len(text) + 1, 0)])[0][0] for it in items}
    items.sort(key=lambda it: first[id(it)])
    _assign_tags(items)
    return {"source": source, "text": text, "local": info, "sent_at": None, **_build(text, items)}


def rebuild(priv: dict, edits: dict | None, settings: dict | None) -> dict:
    """Apply the household's changes: `off` (tags it allows to go out as is), `extra` (more text to hide).
    The known names and the « always mask » list are read again, so a change in Réglages counts."""
    edits = edits or {}
    off = set(edits.get("off") or [])
    text = priv.get("text") or ""
    old = {_key(it["text"]): it for it in priv.get("items") or []}
    keep = [it for it in old.values() if it["source"] in ("rule", "local")]
    fresh = known_items(settings) + [{"text": t.strip(), "kind": guess_kind(t), "source": "manual"}
                                     for t in edits.get("extra") or [] if isinstance(t, str) and t.strip()]
    items = []
    for it in _dedupe(keep + fresh):
        prev = old.get(_key(it["text"]))
        if prev and prev.get("tag"):
            it["tag"] = prev["tag"]
        it = {k: v for k, v in it.items() if k not in ("count", "found", "at")}
        it["on"] = it.get("tag") not in off if it.get("tag") else True
        items.append(it)
    _assign_tags(items)
    return {**priv, **_build(text, items)}


def restore(obj, items: list[dict]):
    """Put the real values back in place of the tags, in every string of the cloud's answer."""
    table = {it["tag"]: it.get("found") or it["text"] for it in items if it.get("tag")}

    def sub(s: str) -> str:
        return LOOSE_TAG_RE.sub(lambda m: table.get(f"[{m.group(1)}_{m.group(2)}]", m.group(0)), s)

    if isinstance(obj, str):
        return sub(obj)
    if isinstance(obj, list):
        return [restore(v, items) for v in obj]
    if isinstance(obj, dict):
        return {k: restore(v, items) for k, v in obj.items()}
    return obj
