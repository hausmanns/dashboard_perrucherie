"""PDF documents: text when the PDF has a text layer, page images when it is a scan.

Rendering uses pypdfium2 (Apache/BSD, wheels for every platform). At most
`MAX_PAGES` pages are kept for the LLM; long statements go page by page in
`documents.py`.
"""
from __future__ import annotations

import io

MAX_PAGES = 6


def parse(data: bytes) -> dict:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    pages = []
    images = []
    try:
        for i, page in enumerate(doc):
            if i >= 40:
                break
            text = page.get_textpage().get_text_range() or ""
            pages.append(text)
            if len(text.strip()) < 40 and len(images) < MAX_PAGES:
                bitmap = page.render(scale=2.0).to_pil()
                buf = io.BytesIO()
                bitmap.convert("RGB").save(buf, format="JPEG", quality=85)
                images.append((buf.getvalue(), "image/jpeg"))
    finally:
        doc.close()
    scanned = sum(len(p.strip()) for p in pages) < 80 * max(1, len(pages))
    return {"kind": "pdf", "engine": "pdf-scan" if scanned else "pdf-text", "account": {},
            "pages": pages, "text": "\n\n".join(f"--- page {i + 1} ---\n{p}" for i, p in enumerate(pages)),
            "images": images, "scanned": scanned, "page_count": len(pages)}


def render_pages(data: bytes, max_pages: int = MAX_PAGES) -> list[tuple[bytes, str]]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    out = []
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            img = page.render(scale=2.0).to_pil()
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            out.append((buf.getvalue(), "image/jpeg"))
    finally:
        doc.close()
    return out
