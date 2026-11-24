# Finances / FIRE — what's left to finalize

The feature is usable as is (see AGENTS.md § 9, tests in `tests/finance/`, 375 passing).
None of the points below blocks testing.

## To do
1. **Tax check against reality** — add the last real tax bill (Documents tab, or
   Réglages → Contrôle). If the gap is > 1 %, adjust `app/finance/params.py`.
   The ESTV calculator API (`swisstaxcalculator.estv.admin.ch`, `API_calculateDetailedTaxes`)
   returned zeros for 2026: retry later with the right field names to add reference vectors
   to `tests/finance/test_tax.py`.
2. **Vaud values marked `# estimated`** in `params.py` (child cap of the quotient familial,
   rent caps of the housing deduction): replace with the official 2026 figures
   (ACI « Instructions générales 2026 ») when found.
3. **Manual tax-bill form** — « + Décision de taxation » in Réglages only jumps to Documents;
   add a small form posting to a new `POST /api/finance/tax-bills`.
4. **Local LLM default** — `LOCAL_LLM_MODEL` defaults to `qwen2.5vl:7b`; either pull it or set
   the model in Réglages → Lecture des documents. Test a real payslip, LPP certificate,
   tax bill and a PDF bank statement end to end, and tune `documents.EXTRACT_PROMPT` if needed.
   Mode « Local masque, le cloud lit » (`private_cloud`): rebuild the image (`docker compose up -d
   --build`) for Tesseract; try it on a real scan — OCR can misread accented capitals and digits.
   Nemotron 3.5 Bolt found names/employer/addresses on a fake slip (~20 s); qwen2.5:3b found nothing.
5. **Bank CSV formats** — UBS / Raiffeisen / ZKB / Yuh / Swissquote profiles were written
   without real files: drop a real export of each, fix `importers/tables.py`, add the file
   (anonymised) as a test fixture.
6. **Monte Carlo** — pension accounts (3a, vested) follow their expected path; making them
   stochastic too would widen the bands slightly (`simulate.monte_carlo`).
7. **Docs** — README (feature list) and TELEGRAM_BOT.md (`/fire`, monthly reminder).
8. **Housekeeping** — FastAPI `@app.on_event` is deprecated (move `main.startup` to a lifespan).
9. **Nice to have** — yearly « bilan » (Wrapped-style) view, dark mode for charts, recurring
   payment detection (subscriptions) from transactions.
