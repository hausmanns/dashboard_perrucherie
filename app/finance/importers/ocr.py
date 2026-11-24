"""OCR on this machine (Tesseract): a scan or a photo becomes text without leaving the house.

Used by the `private_cloud` reading mode, where only text (masked) may go to the
cloud — never an image. Tesseract is installed in the Docker image
(`tesseract-ocr` + French, German, Italian); elsewhere put it on the PATH or
set `TESSERACT_CMD`. `OCR_PSM` sets the page layout mode (default 4: one
column of lines of any size, so a label and its amount stay on one line).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

LANGS_WANTED = ("fra", "deu", "ita", "eng")
MIN_SIDE = 1800  # small photos are enlarged: Tesseract reads letters ~30 px high best


class OCRError(Exception):
    pass


def binary() -> str | None:
    return os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")


_STATUS: dict | None = None


def status(refresh: bool = False) -> dict:
    """{available, version, langs} — cached: the program doesn't change while the app runs."""
    global _STATUS
    if _STATUS is not None and not refresh:
        return _STATUS
    exe = binary()
    out = {"available": False, "version": "", "langs": []}
    if exe:
        try:
            v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            out["version"] = ((v.stdout or v.stderr).splitlines() or [""])[0].strip()
            ls = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=10)
            have = {x.strip() for x in ls.stdout.splitlines()[1:] if x.strip()}
            out["langs"] = [lang for lang in LANGS_WANTED if lang in have]
            out["available"] = bool(out["langs"])
        except (OSError, subprocess.SubprocessError) as e:
            out["error"] = str(e)[:200]
    _STATUS = out
    return out


def _prepare(data: bytes) -> bytes:
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
    except UnidentifiedImageError:
        raise OCRError("image illisible (format HEIC ? exportez-la en JPEG)")
    img = ImageOps.exif_transpose(img).convert("L")  # phone photos carry their rotation in EXIF
    side = max(img.size)
    if side < MIN_SIDE:
        f = MIN_SIDE / side
        img = img.resize((round(img.width * f), round(img.height * f)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def read_image(data: bytes) -> str:
    st = status()
    if not st["available"]:
        raise OCRError("OCR absent : installez Tesseract (inclus dans l'image Docker) pour lire scans et photos")
    psm = os.environ.get("OCR_PSM", "4")
    with tempfile.TemporaryDirectory(prefix="perrucherie-ocr-") as tmp:
        src = Path(tmp) / "page.png"
        src.write_bytes(_prepare(data))
        r = subprocess.run([binary(), str(src), "stdout", "-l", "+".join(st["langs"]), "--psm", psm,
                            "-c", "preserve_interword_spaces=1"], capture_output=True, timeout=180)
    if r.returncode != 0:
        raise OCRError(f"Tesseract : {r.stderr.decode('utf-8', 'replace')[:200]}")
    return r.stdout.decode("utf-8", "replace")


def read_images(images: list[tuple[bytes, str]]) -> str:
    pages = [read_image(data) for data, _mime in images]
    if len(pages) == 1:
        return pages[0]
    return "\n\n".join(f"--- page {i + 1} ---\n{p}" for i, p in enumerate(pages))
