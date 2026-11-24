"""Mode private_cloud: mask here, check, then only the masked text goes to the cloud (no real model is called).

Every name, number and address below is made up."""
import base64
import json

import pytest

from app import config, llm
from app.finance import anonymize, documents
from tests.finance.test_api import onboard


@pytest.fixture()
def client(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.finance import model

    model._CACHE.clear()
    monkeypatch.setattr(documents, "parse_async", lambda doc_id: documents.parse(doc_id))
    return TestClient(main.app)

SLIP = """Hôpital du Lac SA                       Fiche de salaire novembre 2026
Avenue de la Gare 12, 1003 Lausanne
                                        Madame LÉA DUPONT
                                        Chemin des Roses 4b
                                        1020 Renens
N° AVS 756.1234.5678.97     N° personnel: 004512     Née le 15.08.1994
Salaire de base                   7'000.00
AVS/AI/APG 5.3%                    -371.00
LPP                                -412.60
Net à payer                       5'900.00
Versement sur CH93 0076 2011 6238 5295 7
Contact RH : rh@hopital-du-lac.ch, +41 21 555 12 34
Fonds en titres CH0012032048, période du 01.11.2026 au 30.11.2026
"""
NO_LOCAL = {"llm": {"mode": "private_cloud", "local_base_url": "http://local.test/v1"}}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def no_local_model(monkeypatch):
    def chat(messages, settings=None, trace=None, providers=None):
        raise llm.LLMError("local: injoignable")
    monkeypatch.setattr(llm, "chat_json", chat)


def test_rules_hide_identifiers_and_keep_amounts_dates_and_isin(fresh_db, monkeypatch):
    no_local_model(monkeypatch)
    priv = anonymize.prepare(SLIP, NO_LOCAL, "test")
    masked = priv["masked"]
    for secret in ("756.1234.5678.97", "004512", "CH93 0076", "rh@hopital", "555 12 34", "Gare 12",
                   "Roses 4b", "Renens", "15.08.1994"):
        assert secret not in masked, secret
    for kept in ("7'000.00", "-412.60", "5'900.00", "CH0012032048", "01.11.2026", "30.11.2026", "2026"):
        assert kept in masked, kept
    assert priv["check"] == {"ok": True, "leftovers": []}
    assert priv["local"]["engine"] is None and priv["local"]["errors"]  # shown as « règles seules »
    assert "DUPONT" in masked  # no rule knows a name: the local model or the household's list must


def test_known_names_ignore_accents_and_case_but_not_inside_words(fresh_db, monkeypatch):
    from app.finance import db

    no_local_model(monkeypatch)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO members (name, kind, birth_date) VALUES ('Lea', 'adult', '1994-08-15')")
    text = "Madame LÉA Dupont, née le 15.08.1994. Leasing auto 450.00. Lea paie."
    settings = {**NO_LOCAL, "privacy": {"always_mask": ["Dupont"]}}
    priv = anonymize.prepare(text, settings, "test")
    assert priv["masked"] == ("Madame [PERSONNE_1] [PRIVE_1], née le [NAISSANCE_1]. Leasing auto 450.00. "
                              "[PERSONNE_1] paie.")
    by_tag = {it["tag"]: it for it in priv["items"]}
    assert by_tag["[PERSONNE_1]"]["source"] == "known" and by_tag["[PERSONNE_1]"]["count"] == 2
    assert by_tag["[PRIVE_1]"]["source"] == "list"


def test_the_local_model_only_lists_and_bad_items_are_refused(fresh_db, monkeypatch):
    sent = []

    def chat(messages, settings=None, trace=None, providers=None):
        assert providers == ["local"]  # the masking step never goes to the cloud
        sent.append(messages[0]["content"])
        trace.update(attempts=[{"engine": "local:m", "seconds": 0.1, "raw": "{}"}])
        return {"items": [
            {"text": "Madame LÉA DUPONT", "kind": "person"},  # the title is dropped
            {"text": "Hôpital du Lac SA", "kind": "employer"},
            {"text": "Marc Inventé", "kind": "person"},      # not in the text: refused
            {"text": "7'000.00", "kind": "person"},          # an amount: refused
            {"text": "5'900.00", "kind": "id"},              # an amount, even called an id: refused
            {"text": "01.11.2026", "kind": "person"},        # a plain date: refused
            "Renens",                                        # a bare string is accepted too
        ]}, "local:m"

    monkeypatch.setattr(llm, "chat_json", chat)
    priv = anonymize.prepare(SLIP, NO_LOCAL, "test")
    masked = priv["masked"]
    assert "DUPONT" not in masked and "Hôpital du Lac" not in masked and "Renens" not in masked
    assert "7'000.00" in masked and "5'900.00" in masked and "01.11.2026" in masked
    assert priv["local"]["refused"] == 4 and priv["local"]["engine"] == "local:m"
    assert len(sent) == 1 and "7'000.00" in sent[0]  # the local model reads the real text: it stays here
    assert not any(it["text"] == "Marc Inventé" for it in priv["items"])
    assert any(it["text"] == "LÉA DUPONT" and it["source"] == "local" for it in priv["items"])
    assert "Madame [PERSONNE_" in masked


def test_text_is_cut_in_chunks_for_small_local_models(fresh_db, monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "chat_json", lambda m, s=None, trace=None, providers=None: (
        calls.append(len(m[0]["content"])) or ({"items": []}, "local:m")))
    priv = anonymize.prepare("ligne de relevé 12.50\n" * 600, NO_LOCAL, "test")  # ~13'000 characters
    assert priv["local"]["chunks"] == len(calls) >= 4
    assert max(calls) < anonymize.CHUNK + len(anonymize.LOCAL_PROMPT)


def test_a_local_server_that_is_down_is_not_asked_thirty_times(fresh_db, monkeypatch):
    calls = []

    def chat(messages, settings=None, trace=None, providers=None):
        calls.append(1)
        raise llm.LLMError("local: injoignable")

    monkeypatch.setattr(llm, "chat_json", chat)
    priv = anonymize.prepare("x " * 20_000, NO_LOCAL, "test")
    assert len(calls) == 2 and priv["local"]["ok"] == 0
    assert "pas envoyés" in priv["local"]["errors"][-1]


def test_a_long_name_split_by_a_line_break_or_hyphen_is_still_masked(fresh_db, monkeypatch):
    no_local_model(monkeypatch)
    settings = {**NO_LOCAL, "privacy": {"always_mask": ["Hausmannova"]}}
    priv = anonymize.prepare("Titulaire : Hausman-\nnova Léa\nSolde 1'200.00", settings, "test")
    assert "Hausman" not in priv["masked"] and "nova" not in priv["masked"]
    assert "1'200.00" in priv["masked"] and priv["check"]["ok"]


def test_verify_finds_what_is_left_and_blocks(fresh_db, monkeypatch):
    no_local_model(monkeypatch)
    settings = {**NO_LOCAL, "privacy": {"always_mask": ["Dupont"]}}
    priv = anonymize.prepare(SLIP, settings, "test")
    tampered = priv["masked"] + "\nCopie à M. Dupont, IBAN CH56 0483 5012 3456 7800 9"
    check = anonymize.verify(tampered, priv["items"])
    assert not check["ok"]
    assert {x["text"] for x in check["leftovers"]} >= {"Dupont", "CH56 0483 5012 3456 7800 9"}


def test_rebuild_lets_the_household_unmask_and_add(fresh_db, monkeypatch):
    no_local_model(monkeypatch)
    priv = anonymize.prepare(SLIP, NO_LOCAL, "test")
    email = next(it for it in priv["items"] if it["kind"] == "email")
    out = anonymize.rebuild(priv, {"off": [email["tag"]], "extra": ["DUPONT"]}, NO_LOCAL)
    assert "rh@hopital-du-lac.ch" in out["masked"]           # allowed to go out as written
    assert "DUPONT" not in out["masked"]
    # a long word is found inside others too: « Hôpital du Lac » is also hidden in the e-mail
    out2 = anonymize.rebuild(priv, {"off": [email["tag"]], "extra": ["Hôpital du Lac"]}, NO_LOCAL)
    assert "Hôpital du Lac" not in out2["masked"] and "hopital-du-lac" not in out2["masked"]
    assert out["check"]["ok"]                                 # an item turned off is not a leftover
    assert next(it for it in out["items"] if it["text"] == "DUPONT")["source"] == "manual"
    kept = {it["text"]: it["tag"] for it in priv["items"]}
    assert all(kept[it["text"]] == it["tag"] for it in out["items"] if it["text"] in kept)  # tags stay


def test_restore_puts_the_real_values_back_everywhere(fresh_db, monkeypatch):
    no_local_model(monkeypatch)
    settings = {**NO_LOCAL, "privacy": {"always_mask": ["LÉA DUPONT"]}}
    priv = anonymize.prepare(SLIP, settings, "test")
    iban = next(it["tag"] for it in priv["items"] if it["kind"] == "iban")
    answer = {"fields": {"person_name": "[PRIVE_1]", "iban": iban.strip("[]"),
                         "transactions": [{"description": f"Virement {iban}", "amount": -10}]}}
    out = anonymize.restore(answer, priv["items"])
    assert out["fields"]["person_name"] == "LÉA DUPONT"
    assert out["fields"]["iban"] == "CH93 0076 2011 6238 5295 7"          # a tag copied without brackets
    assert out["fields"]["transactions"][0] == {"description": "Virement CH93 0076 2011 6238 5295 7", "amount": -10}


# --------------------------------------------------------------------------- llm.py in private_cloud


def test_private_mode_keeps_every_other_llm_call_local(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "k")
    urls = []

    def post(url, **kw):
        urls.append(url)
        raise llm.httpx.ConnectError("down")

    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(llm.LLMError):
        llm.chat_json(llm.text_message("catégories"), NO_LOCAL)
    assert urls == ["http://local.test/v1/chat/completions"]   # no unmasked fallback to the cloud


def test_cloud_call_uses_the_chosen_model_and_refuses_data_collection(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "k")
    seen = {}

    class R:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": '{"a": 1}'}}]}

    monkeypatch.setattr(llm.httpx, "post", lambda url, json=None, **kw: seen.update(url=url, body=json) or R())
    settings = {"llm": {**NO_LOCAL["llm"], "cloud_model": "some/model"}}
    data, engine = llm.chat_json(llm.text_message("x"), settings, providers=["cloud"])
    assert data == {"a": 1} and engine == "cloud:some/model"
    assert seen["url"] == config.OPENROUTER_URL and seen["body"]["model"] == "some/model"
    assert seen["body"]["provider"] == {"data_collection": "deny"}


# --------------------------------------------------------------------------- the whole flow, through the API


def private(client, monkeypatch, confirm=True, local_ok=True):
    """private_cloud mode, OCR and both models faked. Returns what the cloud received."""
    client.put("/api/finance/settings", json={"llm": {"mode": "private_cloud"}, "privacy": {"confirm": confirm}})
    monkeypatch.setattr(documents.ocr, "read_images", lambda images: SLIP)
    monkeypatch.setattr(documents, "_background", lambda fn, doc_id: fn(doc_id))
    cloud: list[str] = []

    def chat(messages, settings=None, trace=None, providers=None):
        text = messages[0]["content"]
        if providers == ["local"]:
            if not local_ok:
                raise llm.LLMError("local: injoignable")
            return {"items": [{"text": "LÉA DUPONT", "kind": "person"},
                              {"text": "Hôpital du Lac SA", "kind": "employer"}]}, "local:m"
        assert providers == ["cloud"]
        cloud.append(text)
        trace.update(prompt=text, images=0, attempts=[{"engine": "cloud:x", "seconds": 1, "raw": "{}"}])
        person = text[text.index("Madame ") + 7:].split()[0]   # the tag the cloud sees, e.g. [PERSONNE_2]
        return {"doc_type": "payslip", "confidence": 0.9, "summary": f"Fiche de {person}",
                "fields": {"person_name": person, "employer": "[EMPLOYEUR_1]", "period": "2026-11",
                           "gross_monthly": 7000, "net_monthly": 5900, "salaries_per_year": 13}}, "cloud:x"

    monkeypatch.setattr(llm, "chat_json", chat)
    return cloud


def upload(client):
    d = client.post("/api/finance/documents", files=[("files", ("fiche.png", PNG, "image/png"))]).json()[0]
    return client.get(f"/api/finance/documents/{d['id']}").json()


def test_private_flow_waits_for_ok_then_only_masked_text_leaves(client, monkeypatch):
    res = onboard(client)
    cloud = private(client, monkeypatch)
    d = upload(client)
    assert d["status"] == "masked" and cloud == []                 # nothing left before the household's OK
    p = d["privacy"]
    assert p["source"] == "ocr:tesseract" and p["check"]["ok"] and "DUPONT" not in p["masked"]
    assert client.get("/api/finance/documents").json()[0]["has_privacy"] is True

    preview = client.post(f"/api/finance/documents/{d['id']}/mask", json={"extra": ["Renens"]}).json()
    assert "text" not in preview and preview["check"]["ok"]       # the original text is not sent back
    r = client.post(f"/api/finance/documents/{d['id']}/send", json={"extra": ["Lausanne"], "remember": True})
    assert r.status_code == 200, r.text

    (sent,) = cloud
    for secret in ("DUPONT", "LÉA", "Hôpital du Lac", "756.1234", "CH93", "rh@hopital", "Lausanne", "Roses"):
        assert secret not in sent, secret
    assert "7'000.00" in sent and "[PERSONNE_" in sent and "marqueur" in sent

    d = client.get(f"/api/finance/documents/{d['id']}").json()
    assert d["status"] == "review" and d["doc_type"] == "payslip"
    f = d["extracted"]["fields"]
    assert f["person_name"] == "LÉA DUPONT" and f["employer"] == "Hôpital du Lac SA"   # restored here
    assert d["extracted"]["member_id"] == res["members"]["l"]
    assert d["extracted"]["summary"] == "Fiche de LÉA DUPONT"
    assert "masqué (local:m)" in d["engine"] and d["engine"].endswith("cloud:x")
    assert d["debug"]["mask"]["engine"] == "local:m" and d["debug"]["prompt"] == sent   # 🔍 shows what left
    assert d["privacy"]["sent_at"]
    assert client.get("/api/finance/settings").json()["privacy"]["always_mask"] == ["Lausanne"]
    assert client.post(f"/api/finance/documents/{d['id']}/send", json={}).status_code == 409  # sent already


def test_unattended_mode_sends_when_every_check_passed(client, monkeypatch):
    onboard(client)
    cloud = private(client, monkeypatch, confirm=False)
    d = upload(client)
    assert d["status"] == "review" and len(cloud) == 1


def test_unattended_mode_still_waits_when_the_local_model_failed(client, monkeypatch):
    onboard(client)
    cloud = private(client, monkeypatch, confirm=False, local_ok=False)
    d = upload(client)
    assert d["status"] == "masked" and cloud == []
    assert d["privacy"]["local"]["engine"] is None and "règles seules" in d["engine"]


def test_a_cloud_failure_goes_back_to_the_check_screen(client, monkeypatch):
    onboard(client)
    private(client, monkeypatch)
    d = upload(client)

    def down(messages, settings=None, trace=None, providers=None):
        raise llm.LLMError("OpenRouter 503")

    monkeypatch.setattr(llm, "chat_json", down)
    client.post(f"/api/finance/documents/{d['id']}/send", json={})
    d = client.get(f"/api/finance/documents/{d['id']}").json()
    assert d["status"] == "masked" and "503" in d["error"]   # « Vérifier l'envoi » again, to retry


def test_no_ocr_is_a_clear_error(client, monkeypatch):
    onboard(client)
    private(client, monkeypatch)

    def missing(images):
        raise documents.ocr.OCRError("OCR absent : installez Tesseract")

    monkeypatch.setattr(documents.ocr, "read_images", missing)
    d = upload(client)
    assert d["status"] == "error" and "Tesseract" in d["error"]


def test_other_modes_are_unchanged(client, monkeypatch):
    """local_only still sends the image to the local model, as before."""
    onboard(client)
    seen = {}

    def chat(messages, settings=None, trace=None, providers=None):
        seen["providers"] = providers
        seen["images"] = sum(1 for p in messages[0]["content"] if p.get("type") == "image_url")
        return {"doc_type": "other", "fields": {}}, "local:m"

    monkeypatch.setattr(llm, "chat_json", chat)
    client.put("/api/finance/settings", json={"llm": {"mode": "local_only"}})
    d = upload(client)
    assert d["status"] == "review" and d["privacy"] is None
    assert seen == {"providers": None, "images": 1}


@pytest.mark.skipif(not documents.ocr.status()["available"], reason="Tesseract not installed here")
def test_tesseract_reads_a_rendered_page():
    import io

    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1400, 300), "white")
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 42)
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((40, 100), "Salaire brut 7000.00 CHF", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    text = documents.ocr.read_images([(buf.getvalue(), "image/png")])
    assert "7000" in text and "Salaire" in text


def test_privacy_json_survives_the_database(client, monkeypatch):
    onboard(client)
    private(client, monkeypatch)
    d = upload(client)
    from app.finance import db

    with db.get_conn() as conn:
        raw = conn.execute("SELECT privacy FROM documents WHERE id = ?", (d["id"],)).fetchone()["privacy"]
    assert json.loads(raw)["masked"] == d["privacy"]["masked"]
