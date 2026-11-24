"""Drop a document → it is read → you check → it is applied.

1. `store()` keeps the file in `data/finance/docs/` (gitignored), named by its
   SHA-256 (the same file twice is recognised).
2. `parse()` (background thread): bank exports go through the deterministic
   readers (`importers/`); unknown tables get their columns mapped by the LLM;
   PDFs and photos are read by the LLM (text when the PDF has text, page
   images when it is a scan). Result → `extracted`, status `review`. What the
   LLM was sent, its thinking and raw answer → `debug` (🔍 in Documents).
3. `apply()` writes what the household confirmed: transactions + closing
   balance, positions, a salary, the LPP certificate, a 3a balance, a tax bill
   (calibration), a health-insurance premium…

The LLM never writes anything: it only fills `extracted`.

Mode `private_cloud` (one option among the reading modes): a PDF or photo is
turned into text here (PDF text layer, or Tesseract OCR for a scan/photo), the
private details are masked (`anonymize.py`: rules, what the household knows,
the local model's list), the result goes to `privacy`, status `masked`. The
household checks the masked text and calls `send()` (or, with
`privacy.confirm` off, it goes by itself when every check passed). Only that
masked text goes to the cloud; the tags in its answer are put back here, then
status `review` as usual.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import date, datetime
from decimal import Decimal

from .. import llm
from . import anonymize, db, gamify, ledger
from .categorize import normalize
from .importers import ocr, parse_file, tables
from .money import AmountError, parse_amount, parse_date, to_cents

log = logging.getLogger(__name__)

MAX_BYTES = 25 * 1024 * 1024
DOC_TYPES = {
    "bank_statement": "Relevé bancaire",
    "broker_statement": "Relevé de titres",
    "payslip": "Fiche de salaire",
    "salary_certificate": "Certificat de salaire",
    "lpp_certificate": "Certificat de prévoyance (LPP)",
    "pillar3a_statement": "Attestation 3e pilier",
    "vested_statement": "Compte de libre passage",
    "tax_assessment": "Décision de taxation / bordereau",
    "insurance_premium": "Police d'assurance maladie",
    "other": "Autre document",
}

PROMPT_INTRO = "Tu lis un document financier d'un ménage suisse (canton de Vaud)."
PROMPT_RULES = """montants en nombres (pas de texte, pas d'apostrophes), dates ISO AAAA-MM-JJ.
N'invente rien : null quand l'information n'est pas dans le document."""
PROMPT_FIELDS = {
    "payslip": """person_name, employer, period (AAAA-MM), gross_monthly, net_monthly, work_pct, salaries_per_year
  (12 ou 13), avs_ai_apg, ac, aanp, ijm, lpp_employee, family_allowance, currency""",
    "salary_certificate": """person_name, employer, year, gross_total (chiffre 8), net_total (chiffre 11),
  avs_ai_apg_ac_aanp (chiffre 9), lpp_ordinary (chiffre 10.1), lpp_buyin (chiffre 10.2), free_transport (bool, lettre F),
  canteen (bool, lettre G), work_pct""",
    "lpp_certificate": """person_name, fund, date, savings_balance (avoir de vieillesse), savings_credits_annual
  (bonifications de vieillesse par an, salarié + employeur ; selon la caisse « cotisation d'épargne par an »,
  « bonifications d'épargne », « Altersgutschriften », « Sparbeitrag » ; épargne seulement, jamais la cotisation de
  risque), employee_annual (cotisation totale du salarié par an, épargne + risque = déduction mensuelle × 12),
  employer_annual (idem, part employeur), insured_salary, projected_capital_65, projected_pension_65 (rente annuelle),
  conversion_rate_pct, buyin_potential (rachat possible)""",
    "pillar3a_statement": "person_name, provider, date, balance, contributions_year, currency",
    "vested_statement": "person_name, provider, date, balance, contributions_year, currency",
    "bank_statement": """person_name (titulaire), bank, iban, currency, period_start, period_end, opening_balance,
  closing_balance, transactions (liste {date, amount (négatif = débit), description})""",
    "broker_statement": """person_name (titulaire), broker, date, currency, total_value, cash, holdings (liste {name,
  symbol (ticker boursier seulement s'il est imprimé, sinon null, jamais le nom), isin (12 caractères, ex.
  CH0012032048), valor (n° de valeur suisse : « Valor », « Valor-Nr. », « N° de valeur », chiffres seulement),
  quantity (nombre de titres), price (cours d'UN titre), value (valeur de la ligne = quantity × price),
  currency (code ISO de la cotation : CHF, USD, EUR…), asset_class (equity, bonds, crypto, real_estate, commodity
  ou cash)})""",
    "tax_assessment": """person_name, year, authority (ICC ou IFD ou les deux), taxable_income (revenu imposable ICC),
  rate_income (revenu déterminant pour le taux), taxable_wealth, icc_amount (impôt cantonal + communal),
  ifd_amount, ifd_taxable_income""",
    "insurance_premium": "person_name, insurer, year, monthly_premium, franchise, model",
    "other": "description",
}
PROMPT_ANSWER = """Réponds uniquement en JSON : {"doc_type": "%s", "confidence": 0.0-1.0, "summary": "une phrase en
français", "fields": {...}}"""
_FIELD_LINES = "\n".join(f"- {k}: {v}" for k, v in PROMPT_FIELDS.items())

EXTRACT_PROMPT = f"""{PROMPT_INTRO}
1) Identifie son type : payslip (fiche de salaire mensuelle), salary_certificate (certificat de salaire annuel,
formulaire 11), lpp_certificate (certificat de prévoyance / caisse de pension), pillar3a_statement (attestation ou
relevé 3e pilier A), vested_statement (libre passage), bank_statement (relevé de compte), broker_statement (relevé
de dépôt titres), tax_assessment (décision de taxation ou bordereau ICC/IFD), insurance_premium (police ou facture
d'assurance maladie LAMal), other.
Attention : un « relevé fiscal » ou « état des titres » envoyé par une banque ou un courtier (liste de titres,
comptes, cryptos et leurs valeurs) est un broker_statement. tax_assessment vient de l'administration fiscale et
donne l'impôt à payer.
2) Extrais les champs du type, {PROMPT_RULES}

Champs par type :
{_FIELD_LINES}

{PROMPT_ANSWER % "..."}"""


def extract_prompt(doc_type: str = "") -> str:
    """The full prompt (the model guesses the type), or, when the household chose the type,
    a short one with only that type's fields — easier for a small local model."""
    if doc_type not in PROMPT_FIELDS:
        return EXTRACT_PROMPT
    return f"""{PROMPT_INTRO}
Le ménage a indiqué son type : {doc_type} ({DOC_TYPES[doc_type]}). Ne le remets pas en question.
Extrais ces champs, {PROMPT_RULES}

Champs : {PROMPT_FIELDS[doc_type]}

{PROMPT_ANSWER % doc_type}"""

PRIVATE_NOTE = """Les données personnelles ont été remplacées par des marqueurs entre crochets ([PERSONNE_1],
[IBAN_1], [ADRESSE_1]…). Quand la valeur d'un champ est masquée, recopie le marqueur tel quel
(ex. "person_name": "[PERSONNE_1]")."""
OCR_NOTE = """Le texte vient d'une lecture OCR d'un scan ou d'une photo : il peut contenir des fautes de lecture
et des colonnes décalées. Un montant illisible vaut null."""
PRIVATE_MAX_CHARS = 30_000

MAPPING_PROMPT = """Voici le début d'un export bancaire (tableau). Indique quelle colonne contient quoi.
Lignes (index: cellules séparées par |) :
{rows}

Réponds en JSON : {{"header_index": <index de la ligne d'en-tête>, "mapping": {{"date": <col>, "amount": <col ou null>,
"debit": <col ou null>, "credit": <col ou null>, "description": <col>, "counterparty": <col ou null>,
"currency": <col ou null>, "balance": <col ou null>}}}} (colonnes numérotées depuis 0)."""


# --------------------------------------------------------------------------- store / list


def check_type(doc_type: str | None) -> str:
    """'' = let the model guess; otherwise one of DOC_TYPES."""
    doc_type = (doc_type or "").strip()
    if doc_type and doc_type not in DOC_TYPES:
        raise ValueError(f"type de document inconnu : {doc_type}")
    return doc_type


def set_hint(doc_id: int, doc_type: str | None) -> None:
    _set(doc_id, hint_type=check_type(doc_type))


def store(filename: str, data: bytes, mime: str = "", member_id: int | None = None,
          account_id: int | None = None, hint_type: str = "") -> dict:
    """`hint_type`: the type the household chose at upload ('' = the model guesses)."""
    hint_type = check_type(hint_type)
    if len(data) > MAX_BYTES:
        raise ValueError("fichier trop lourd (25 Mo max)")
    sha = hashlib.sha256(data).hexdigest()
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin")[:8]
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha,)).fetchone()
        if row:
            return {**_doc(dict(row)), "duplicate": True}
        stored = f"{sha[:32]}.{ext}"
        (db.docs_dir() / stored).write_bytes(data)
        cur = conn.execute(
            "INSERT INTO documents (filename, stored_name, mime, size, sha256, member_id, account_id, hint_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (filename[:200], stored, mime[:100], len(data), sha, member_id, account_id, hint_type))
        doc_id = cur.lastrowid
    gamify.award("document", str(doc_id), 25, "Document ajouté")
    return get(doc_id)


def _doc(row: dict) -> dict:
    row = dict(row)
    row["extracted"] = db.loads(row.get("extracted"), None)
    for k in ("debug", "privacy"):
        if k in row:
            row[k] = db.loads(row[k], None)
    row["doc_type_label"] = DOC_TYPES.get(row.get("doc_type") or "", "")
    row["hint_type"] = row.get("hint_type") or ""
    return row


def get(doc_id: int) -> dict:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        raise KeyError(doc_id)
    return _doc(dict(row))


def list_docs(limit: int = 100) -> list[dict]:
    with db.get_conn() as conn:
        rows = db.rows(conn.execute("SELECT * FROM documents ORDER BY uploaded_at DESC, id DESC LIMIT ?", (limit,)))
    out = []
    for r in rows:
        r["has_debug"] = bool(r.pop("debug", None))  # the trace itself only comes with GET /documents/{id}
        r["has_privacy"] = bool(r.pop("privacy", None))
        d = _doc(r)
        ex = d.get("extracted") or {}
        if ex.get("transactions") and len(ex["transactions"]) > 5:
            ex = {**ex, "transactions": ex["transactions"][:5], "transactions_count": len(ex["transactions"])}
            d["extracted"] = ex
        out.append(d)
    return out


def delete(doc_id: int) -> None:
    d = get(doc_id)
    path = db.docs_dir() / d["stored_name"]
    if path.exists():
        path.unlink()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def _set(doc_id: int, **fields) -> None:
    for k in ("extracted", "debug", "privacy"):
        if k in fields and not isinstance(fields[k], (str, type(None))):
            fields[k] = json.dumps(fields[k], default=_json_default)
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db.get_conn() as conn:
        conn.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*fields.values(), doc_id))


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, bytes):
        return None
    raise TypeError(type(o))


# --------------------------------------------------------------------------- parse


def _background(fn, doc_id: int) -> None:
    threading.Thread(target=fn, args=(doc_id,), daemon=True, name=f"finance-doc-{doc_id}").start()


def parse_async(doc_id: int) -> None:
    _background(parse, doc_id)


def parse(doc_id: int) -> dict:
    d = get(doc_id)
    _set(doc_id, status="parsing", error="", debug=None, privacy=None)
    trace: dict = {}  # filled by the LLM call, kept even when it fails
    try:
        data = (db.docs_dir() / d["stored_name"]).read_bytes()
        settings = db.get_settings()
        parsed = parse_file(d["filename"], data)
        if parsed["kind"] in ("pdf", "image") and llm.resolve(settings)["mode"] == "private_cloud":
            return _mask_document(doc_id, parsed, data, settings)
        result = _parse(d, data, trace, parsed, settings)
        _set(doc_id, status="review", extracted=result, engine=result.get("engine", ""),
             doc_type=result.get("doc_type", ""), parsed_at=datetime.now().isoformat(timespec="seconds"),
             debug=trace or None)
    except Exception as e:  # noqa: BLE001 — every failure is shown to the user
        log.exception("document %s", doc_id)
        _set(doc_id, status="error", error=str(e)[:500], debug=trace or None)
    return get(doc_id)


def _match_account(hint: dict) -> int | None:
    iban = (hint or {}).get("iban", "").replace(" ", "").upper()
    if not iban:
        return None
    with db.get_conn() as conn:
        for r in conn.execute("SELECT id, iban FROM accounts WHERE iban != ''"):
            if r["iban"].replace(" ", "").upper() == iban:
                return r["id"]
    return None


def _match_member(name: str | None) -> int | None:
    if not name:
        return None
    key = normalize(name)
    with db.get_conn() as conn:
        people = db.rows(conn.execute("SELECT id, name FROM members WHERE kind = 'adult'"))
    hits = [p["id"] for p in people if normalize(p["name"]) and normalize(p["name"]).split()[0] in key.split()]
    return hits[0] if len(hits) == 1 else None


def _tx_out(t: dict) -> dict:
    return {**t, "amount": float(t["amount"]), "balance": float(t["balance"]) if t.get("balance") is not None else None}


def _parse(d: dict, data: bytes, trace: dict | None = None, parsed: dict | None = None,
           settings: dict | None = None) -> dict:
    parsed = parsed or parse_file(d["filename"], data)
    settings = settings or db.get_settings()
    kind = parsed["kind"]
    if kind == "table":
        rows = [parsed.get("columns", [])] + parsed.get("rows", [])
        shown = "\n".join(f"{i}: " + " | ".join(r) for i, r in enumerate(rows[:12]))
        mapping, engine = llm.chat_json(llm.text_message(MAPPING_PROMPT.format(rows=shown)), settings, trace=trace)
        all_rows = tables.read_rows(d["filename"], data)
        parsed = tables.apply_mapping(all_rows, int(mapping.get("header_index", 0)),
                                      {k: v for k, v in (mapping.get("mapping") or {}).items() if v is not None})
        parsed["engine"] = f"csv:llm-mapping ({engine})"
        kind = parsed["kind"]
    if kind == "transactions":
        return {
            "doc_type": "bank_statement", "engine": parsed["engine"], "confidence": 1.0,
            "summary": f"{len(parsed['transactions'])} opérations",
            "account_hint": parsed.get("account", {}), "account_id": _match_account(parsed.get("account", {})),
            "transactions": [_tx_out(t) for t in parsed["transactions"]],
            "balances": [{**b, "amount": float(b["amount"])} for b in parsed.get("balances", [])],
            "skipped": parsed.get("skipped", 0),
        }
    if kind == "holdings":
        hs = [{**h, **{k: float(h[k]) if h.get(k) is not None else None for k in ("quantity", "price", "value")}}
              for h in parsed["holdings"]]
        return {"doc_type": "broker_statement", "engine": parsed["engine"], "confidence": 1.0,
                "summary": f"{len(hs)} positions", "account_hint": parsed.get("account", {}),
                "account_id": _match_account(parsed.get("account", {})), "fields": {"holdings": hs}}
    if kind in ("pdf", "image"):
        hint = d.get("hint_type") or ""
        prompt = extract_prompt(hint)
        if kind == "pdf" and not parsed.get("scanned"):
            text = parsed["text"][:30_000]
            messages = llm.text_message(prompt + "\n\nTexte du document :\n" + text)
        else:
            images = parsed.get("images") or []
            if kind == "pdf" and not images:
                from .importers.pdf import render_pages
                images = render_pages(data)
            messages = llm.image_message(prompt, images[:6])
        answer, engine = llm.chat_json(messages, settings, trace=trace)
        return _from_llm(answer, f"{parsed['engine']} → {engine}", forced_type=hint)
    raise ValueError("format non reconnu — essayez l'export CAMT.053 (XML), CSV ou PDF")


# --------------------------------------------------------------------------- private_cloud: mask, check, send


def _document_text(parsed: dict, data: bytes) -> tuple[str, str]:
    """(text, source): the PDF's own text, or OCR here for a scan or a photo — never an image to the cloud."""
    if parsed["kind"] == "pdf" and not parsed.get("scanned"):
        return parsed["text"], "pdf-text"
    if parsed["kind"] == "pdf":
        from .importers.pdf import render_pages
        images = render_pages(data, scale=3.0)
    else:
        images = parsed.get("images") or []
    try:
        text = ocr.read_images(images)
    except ocr.OCRError as e:
        raise ValueError(str(e))
    return text, "ocr:tesseract"


def _mask_document(doc_id: int, parsed: dict, data: bytes, settings: dict) -> dict:
    text, source = _document_text(parsed, data)
    if len(text.strip()) < 20:
        raise ValueError("aucun texte lisible dans ce document (photo floue ?)")
    priv = anonymize.prepare(text[:PRIVATE_MAX_CHARS], settings, source)
    who = priv["local"]["engine"] or "règles seules"
    _set(doc_id, status="masked", privacy=priv, engine=f"{source} → masqué ({who})", error="",
         parsed_at=datetime.now().isoformat(timespec="seconds"))
    unattended = not (settings.get("privacy") or {}).get("confirm", True)
    if unattended and priv["check"]["ok"] and priv["local"]["engine"] and not priv["local"]["errors"]:
        _set(doc_id, status="parsing")
        _send_now(doc_id)  # already in the background thread
    return get(doc_id)


def _privacy_of(doc_id: int) -> tuple[dict, dict]:
    d = get(doc_id)
    if not d.get("privacy"):
        raise ValueError("rien à envoyer : ce document n'a pas été masqué (mode « Local masque, le cloud lit »)")
    return d, d["privacy"]


def mask_preview(doc_id: int, edits: dict | None = None) -> dict:
    """What `send()` would send with these changes (`off`: tags left visible, `extra`: more text to hide).
    Nothing is saved, nothing is sent."""
    _d, priv = _privacy_of(doc_id)
    out = anonymize.rebuild(priv, edits, db.get_settings())
    return {k: v for k, v in out.items() if k != "text"}


def send(doc_id: int, edits: dict | None = None) -> dict:
    """The household checked the masked text: send it to the cloud model (in the background).
    Raises ValueError when the final check still finds a private detail — then nothing leaves."""
    edits = edits or {}
    d, priv = _privacy_of(doc_id)
    if d["status"] != "masked":
        raise ValueError("ce document n'attend pas d'envoi")
    if edits.get("remember") and edits.get("extra"):
        settings = db.get_settings()
        have = list((settings.get("privacy") or {}).get("always_mask") or [])
        known = {anonymize._key(t) for t in have}
        have += [t.strip() for t in edits["extra"] if t.strip() and anonymize._key(t) not in known]
        db.save_settings({"privacy": {"always_mask": have}})
    priv = anonymize.rebuild(priv, edits, db.get_settings())
    _set(doc_id, privacy=priv)
    if not priv["check"]["ok"]:
        left = ", ".join(f"« {x['text']} »" for x in priv["check"]["leftovers"][:5])
        raise ValueError(f"contrôle final : encore visible {left} — masquez-le ou décochez-le, rien n'est parti")
    _set(doc_id, status="parsing", error="")
    _background(_send_now, doc_id)
    return get(doc_id)


def _send_now(doc_id: int) -> None:
    d = get(doc_id)
    priv = d["privacy"]
    trace: dict = {}
    try:
        check = anonymize.verify(priv["masked"], priv["items"])  # the exact text about to leave, once more
        if not check["ok"]:
            raise ValueError("contrôle final : " + ", ".join(x["text"] for x in check["leftovers"][:5]))
        hint = d.get("hint_type") or ""
        note = PRIVATE_NOTE + ("\n" + OCR_NOTE if priv["source"].startswith("ocr") else "")
        messages = llm.text_message(f"{extract_prompt(hint)}\n\n{note}\n\nTexte du document :\n{priv['masked']}")
        answer, engine = llm.chat_json(messages, db.get_settings(), trace=trace, providers=["cloud"])
        answer = anonymize.restore(answer, priv["items"])
        who = priv["local"]["engine"] or "règles seules"
        result = _from_llm(answer, f"{priv['source']} → masqué ({who}) → {engine}", forced_type=hint)
        result["privacy"] = {"source": priv["source"], "masked": sum(1 for it in priv["items"] if it.get("on"))}
        trace["mask"] = {k: priv["local"].get(k) for k in ("engine", "chunks", "ok", "errors", "seconds", "refused",
                                                           "attempts")}
        sent_at = datetime.now().isoformat(timespec="seconds")
        _set(doc_id, status="review", extracted=result, engine=result["engine"], doc_type=result["doc_type"],
             parsed_at=sent_at, debug=trace, privacy={**priv, "sent_at": sent_at})
    except Exception as e:  # noqa: BLE001 — shown on the « before sending » screen, which can send again
        log.exception("document %s (envoi masqué)", doc_id)
        _set(doc_id, status="masked", error=f"envoi : {e}"[:500], debug=trace or None)


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(parse_amount(v))
    except (AmountError, TypeError, ValueError):
        return None


def _from_llm(answer: dict, engine: str, forced_type: str = "") -> dict:
    """`forced_type`: chosen by the household, it wins over the model's guess."""
    if forced_type in DOC_TYPES:
        doc_type = forced_type
    else:
        doc_type = answer.get("doc_type") if answer.get("doc_type") in DOC_TYPES else "other"
    fields = answer.get("fields") or {}
    out = {"doc_type": doc_type, "engine": engine, "confidence": float(answer.get("confidence") or 0.5),
           "summary": str(answer.get("summary") or "")[:300], "fields": fields,
           "member_id": _match_member(fields.get("person_name"))}
    if doc_type == "bank_statement":
        txs = []
        for t in fields.get("transactions") or []:
            try:
                txs.append({"date": parse_date(t.get("date")), "amount": float(parse_amount(t.get("amount"))),
                            "currency": fields.get("currency") or "CHF", "description": str(t.get("description") or ""),
                            "counterparty": "", "counterparty_iban": "", "reference": ""})
            except (ValueError, AmountError, TypeError):
                continue
        out["transactions"] = txs
        out["account_hint"] = {"iban": (fields.get("iban") or "").replace(" ", ""), "currency": fields.get("currency")}
        out["account_id"] = _match_account(out["account_hint"])
        bals = []
        if _num(fields.get("closing_balance")) is not None and fields.get("period_end"):
            try:
                bals.append({"type": "closing", "date": parse_date(fields["period_end"]),
                             "amount": _num(fields["closing_balance"])})
            except ValueError:
                pass
        out["balances"] = bals
    elif doc_type == "broker_statement":
        fields["holdings"] = _clean_holdings(fields)
    return out


def _ids(isin, valor) -> tuple[str, str]:
    """(ISIN, Valor). A Swiss statement often prints only the Valor (« 1'203'204 »): digits, which
    small models put in `isin`. A Valor is not an ISIN: keep each in its own place, drop what is neither."""
    def text(v) -> str:  # a model may answer 1203204 or 1203204.0 as a number
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v or "")
    isin = re.sub(r"[\s'’.\-]", "", text(isin)).upper()
    valor = re.sub(r"[\s'’.\-]", "", text(valor))
    if isin.isdigit() and not valor:
        valor = isin
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
        isin = ""
    if not re.fullmatch(r"\d{3,12}", valor):
        valor = ""
    return isin, valor


ISO_CURRENCIES = {"CHF", "EUR", "USD", "GBP", "JPY", "CAD", "AUD", "SEK", "NOK", "DKK", "HKD", "SGD", "CNY", "NZD",
                  "PLN", "CZK"}
ASSET_CLASSES = {"equity", "bonds", "cash", "real_estate", "crypto", "commodity"}


def _clean_holdings(fields: dict) -> list[dict]:
    """Small models fill every slot they are given. On a Yuh tax statement (no tickers printed)
    Nemotron 3.5 Bolt copied the name into `symbol` (« ROCHE GS »: no live price can be found),
    the share price into `value`, and a coin's own code (« SWQ ») into `currency`."""
    doc_cur = str(fields.get("currency") or "").strip().upper()
    doc_cur = doc_cur if doc_cur in ISO_CURRENCIES else "CHF"
    out = []
    for h in fields.get("holdings") or []:
        if not isinstance(h, dict):
            continue
        h = dict(h)
        name = str(h.get("name") or "").strip()
        sym = str(h.get("symbol") or "").strip().upper()
        if " " in sym or (name and sym == name.upper()):
            sym = ""  # a name, not a ticker: better none than a wrong one
        h["symbol"] = sym
        h["isin"], h["valor"] = _ids(h.get("isin"), h.get("valor"))
        if h.get("asset_class") not in ASSET_CLASSES:
            h.pop("asset_class", None)
        cur = str(h.get("currency") or "").strip().upper()
        if cur not in ISO_CURRENCIES:
            if cur and cur == sym:  # « 930 SWQ »: counted in its own coin
                h.setdefault("asset_class", "crypto")
            cur = doc_cur
        h["currency"] = cur
        q, p, v = _num(h.get("quantity")), _num(h.get("price")), _num(h.get("value"))
        if q and p and v and abs(q - 1) > 0.01 and abs(v - p) <= 0.005 * abs(p):
            v = round(q * p, 2)  # the share price was copied as the line's value
        h.update(quantity=q, price=p, value=v)
        out.append(h)
    return out


# --------------------------------------------------------------------------- apply


def apply(doc_id: int, body: dict) -> dict:
    """Write a reviewed document. `body` may override: account_id, member_id, fields, create_account."""
    d = get(doc_id)
    ex = d.get("extracted") or {}
    doc_type = body.get("doc_type") or ex.get("doc_type")
    fields = {**(ex.get("fields") or {}), **(body.get("fields") or {})}
    member_id = body.get("member_id") or ex.get("member_id") or d.get("member_id")
    account_id = body.get("account_id") or ex.get("account_id") or d.get("account_id")
    result: dict = {"doc_type": doc_type}

    if body.get("create_account") and not account_id:
        account_id = _create_account(body["create_account"], ex, member_id)
        result["account_created"] = account_id

    if doc_type == "bank_statement":
        if not account_id:
            raise ValueError("choisissez le compte de ce relevé")
        txs = [{**t, "amount": Decimal(str(t["amount"]))} for t in ex.get("transactions", [])]
        result["import"] = ledger.import_transactions(int(account_id), txs, doc_id)
        for b in ex.get("balances", []):
            if b.get("type") == "closing" and b.get("date"):
                _snapshot(int(account_id), b["date"], b["amount"], "document", doc_id)
                result["balance"] = b
        hint = ex.get("account_hint") or {}
        if hint.get("iban"):
            with db.get_conn() as conn:
                conn.execute("UPDATE accounts SET iban = ? WHERE id = ? AND iban = ''", (hint["iban"], account_id))
    elif doc_type == "broker_statement":
        if not account_id:
            raise ValueError("choisissez le compte titres")
        result["holdings"] = _apply_holdings(int(account_id), fields.get("holdings") or [], fields)
    elif doc_type == "payslip":
        result["income"] = _apply_payslip(member_id, fields, doc_id)
    elif doc_type == "salary_certificate":
        result["income"] = _apply_certificate(member_id, fields, doc_id)
    elif doc_type in ("lpp_certificate", "pillar3a_statement", "vested_statement"):
        kind = {"lpp_certificate": "lpp", "pillar3a_statement": "pillar3a", "vested_statement": "vested"}[doc_type]
        account_id = account_id or _pension_account(kind, member_id, fields)
        bal = _num(fields.get("savings_balance") if kind == "lpp" else fields.get("balance"))
        day = _iso(fields.get("date")) or date.today().isoformat()
        if bal is not None:
            _snapshot(int(account_id), day, bal, "document", doc_id)
        if kind == "lpp":
            meta_keys = {"savings_credits_annual": "savings_credits_annual", "employee_annual": "employee_annual",
                         "employer_annual": "employer_annual", "conversion_rate_pct": "conversion_pct",
                         "buyin_potential": "buyin_potential", "projected_capital_65": "projected_65",
                         "projected_pension_65": "projected_pension_65", "insured_salary": "insured_salary"}
            patch = {dst: _num(fields.get(src)) for src, dst in meta_keys.items() if _num(fields.get(src)) is not None}
            patch["certificate_date"] = day
            _patch_meta("accounts", int(account_id), patch)
        result["account_id"] = account_id
        result["balance"] = bal
    elif doc_type == "tax_assessment":
        result["tax_bill"] = _apply_tax(member_id, fields, doc_id)
    elif doc_type == "insurance_premium":
        monthly = _num(fields.get("monthly_premium"))
        if member_id and monthly:
            _patch_meta("members", int(member_id), {"insurance_annual": round(monthly * 12, 2)})
        result["insurance_annual"] = monthly * 12 if monthly else None
    _set(doc_id, status="applied", applied_at=datetime.now().isoformat(timespec="seconds"),
         member_id=member_id, account_id=account_id, doc_type=doc_type or "")
    gamify.award("document_applied", str(doc_id), 25, DOC_TYPES.get(doc_type or "other", "Document"))
    gamify.check_badges()
    return result


def _iso(v) -> str | None:
    try:
        return parse_date(v) if v else None
    except ValueError:
        return None


def _snapshot(account_id: int, day: str, amount: float, source: str, doc_id: int | None) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO snapshots (account_id, date, balance_cents, source, document_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, date) DO UPDATE SET balance_cents = excluded.balance_cents, "
            "source = excluded.source, document_id = excluded.document_id",
            (account_id, day, to_cents(amount), source, doc_id))


def _patch_meta(table: str, row_id: int, patch: dict) -> None:
    with db.get_conn() as conn:
        row = conn.execute(f"SELECT meta FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if not row:
            return
        meta = db.loads(row["meta"], {})
        meta.update({k: v for k, v in patch.items() if v is not None})
        conn.execute(f"UPDATE {table} SET meta = ? WHERE id = ?", (json.dumps(meta), row_id))


def _create_account(spec: dict, ex: dict, member_id) -> int:
    """`spec["owners"]`, when given, wins — even {} (shared equally): the household chose."""
    hint = ex.get("account_hint") or {}
    owners = spec["owners"] if isinstance(spec.get("owners"), dict) else {str(member_id): 1.0} if member_id else {}
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (name, institution, kind, currency, owners, iban, valuation) VALUES (?,?,?,?,?,?,?)",
            (spec.get("name") or hint.get("institution") or "Nouveau compte", spec.get("institution") or
             hint.get("institution") or "", spec.get("kind") or "current",
             (spec.get("currency") or hint.get("currency") or "CHF").upper(), json.dumps(owners),
             hint.get("iban") or "", spec.get("valuation") or "manual"))
        return cur.lastrowid


def _pension_account(kind: str, member_id, fields: dict) -> int:
    with db.get_conn() as conn:
        for r in db.rows(conn.execute("SELECT * FROM accounts WHERE kind = ? AND archived = 0", (kind,))):
            owners = db.loads(r["owners"], {})
            if member_id and owners.get(str(member_id)) or (not member_id and not owners):
                return r["id"]
    name = {"lpp": "Caisse de pension", "pillar3a": "3e pilier", "vested": "Libre passage"}[kind]
    provider = fields.get("fund") or fields.get("provider") or ""
    return _create_account({"name": f"{name} {provider}".strip(), "kind": kind, "institution": provider,
                            "owners": {str(member_id): 1.0} if member_id else {}}, {}, member_id)


def _apply_holdings(account_id: int, holdings: list[dict], fields: dict) -> dict:
    n = 0
    with db.get_conn() as conn:
        conn.execute("UPDATE accounts SET valuation = 'holdings' WHERE id = ?", (account_id,))
        for h in holdings:
            qty = _num(h.get("quantity"))
            if qty is None:
                continue
            sym, name = (h.get("symbol") or "").strip(), (h.get("name") or "").strip()
            isin, valor = (h.get("isin") or "").strip(), (h.get("valor") or "").strip()
            price = _num(h.get("price"))
            if price is None and _num(h.get("value")) and qty:
                price = _num(h.get("value")) / qty
            existing = conn.execute(
                "SELECT id, symbol FROM holdings WHERE account_id = ? AND ((symbol != '' AND symbol = ?) "
                "OR (isin != '' AND isin = ?) OR (valor != '' AND valor = ?) OR name = ?)",
                (account_id, sym, isin, valor, name or sym)).fetchone()
            # a ticker typed by hand is kept when the statement prints none
            sym = sym or (existing["symbol"] if existing else "")
            vals = (sym, isin, valor, name or sym or isin or valor, qty, (h.get("currency") or "CHF").upper()[:3],
                    h.get("asset_class") or "equity", price)
            if existing:
                conn.execute("UPDATE holdings SET symbol=?, isin=?, valor=?, name=?, quantity=?, currency=?, "
                             "asset_class=?, manual_price=?, updated_at=datetime('now') WHERE id = ?",
                             (*vals, existing["id"]))
            else:
                conn.execute("INSERT INTO holdings (account_id, symbol, isin, valor, name, quantity, currency, "
                             "asset_class, manual_price) VALUES (?,?,?,?,?,?,?,?,?)", (account_id, *vals))
            n += 1
    if _num(fields.get("cash")) is not None:
        _patch_meta("accounts", account_id, {"cash": _num(fields.get("cash"))})
    from . import metrics
    metrics.write_auto_snapshots()
    return {"positions": n}


def _salary_row(member_id) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM incomes WHERE member_id = ? AND kind = 'salary' "
                           "AND (end_date IS NULL OR end_date >= date('now')) ORDER BY gross_annual_cents DESC LIMIT 1",
                           (member_id,)).fetchone()
    return dict(row) if row else None


def _upsert_salary(member_id, values: dict, doc_id) -> dict:
    if not member_id:
        raise ValueError("à qui appartient ce document ? choisissez la personne")
    row = _salary_row(member_id)
    cols = {k: v for k, v in values.items() if v is not None}
    cols["document_id"] = doc_id
    with db.get_conn() as conn:
        if row:
            sets = ", ".join(f"{k} = ?" for k in cols)
            conn.execute(f"UPDATE incomes SET {sets}, updated_at = datetime('now') WHERE id = ?",
                         (*cols.values(), row["id"]))
            rid = row["id"]
        else:
            cols.setdefault("kind", "salary")
            cols["member_id"] = member_id
            keys = ", ".join(cols)
            cur = conn.execute(f"INSERT INTO incomes ({keys}) VALUES ({', '.join('?' * len(cols))})",
                               tuple(cols.values()))
            rid = cur.lastrowid
    return {"income_id": rid, **values}


def _pos(v):
    """A salary amount or deduction, always positive: payslips print deductions as -412.60 and
    some models (Nemotron 3.5 Bolt…) copy the minus sign."""
    n = _num(v)
    return abs(n) if n is not None else None


def _apply_payslip(member_id, f: dict, doc_id) -> dict:
    per_year = int(_pos(f.get("salaries_per_year")) or 12)
    gross_m = _pos(f.get("gross_monthly"))
    vals = {
        "gross_annual_cents": to_cents(gross_m * per_year) if gross_m else None,
        "net_annual_cents": to_cents(_pos(f.get("net_monthly")) * per_year) if _pos(f.get("net_monthly")) else None,
        "work_pct": _pos(f.get("work_pct")),
        "lpp_employee_annual_cents": to_cents(_pos(f.get("lpp_employee")) * 12) if _pos(f.get("lpp_employee")) else None,
        "employer": (f.get("employer") or None),
    }
    if gross_m and _pos(f.get("aanp")):
        _patch_meta("members", int(member_id), {"aanp_rate": round(_pos(f.get("aanp")) / gross_m, 4)})
    return _upsert_salary(member_id, vals, doc_id)


def _apply_certificate(member_id, f: dict, doc_id) -> dict:
    vals = {
        "gross_annual_cents": to_cents(_pos(f.get("gross_total"))) if _pos(f.get("gross_total")) else None,
        "net_annual_cents": to_cents(_pos(f.get("net_total"))) if _pos(f.get("net_total")) else None,
        "lpp_employee_annual_cents": to_cents(_pos(f.get("lpp_ordinary"))) if _pos(f.get("lpp_ordinary")) else None,
        "work_pct": _num(f.get("work_pct")),
        "employer": f.get("employer") or None,
    }
    if member_id:
        patch = {}
        if f.get("canteen") is True:
            patch["canteen"] = True
        if f.get("free_transport") is True:
            patch["commute"] = 0
        if patch:
            _patch_meta("members", int(member_id), patch)
    return _upsert_salary(member_id, vals, doc_id)


def _apply_tax(member_id, f: dict, doc_id) -> dict:
    year = int(_num(f.get("year")) or date.today().year - 1)
    vals = (member_id, year,
            to_cents(_num(f.get("taxable_income"))) if _num(f.get("taxable_income")) is not None else None,
            to_cents(_num(f.get("rate_income"))) if _num(f.get("rate_income")) is not None else None,
            to_cents(_num(f.get("taxable_wealth"))) if _num(f.get("taxable_wealth")) is not None else None,
            to_cents(_num(f.get("icc_amount"))) if _num(f.get("icc_amount")) is not None else None,
            to_cents(_num(f.get("ifd_amount"))) if _num(f.get("ifd_amount")) is not None else None, doc_id)
    with db.get_conn() as conn:
        cur = conn.execute("INSERT INTO tax_bills (member_id, year, taxable_income_cents, rate_income_cents, "
                           "taxable_wealth_cents, icc_cents, ifd_cents, document_id) VALUES (?,?,?,?,?,?,?,?)", vals)
    return {"id": cur.lastrowid, "year": year}
