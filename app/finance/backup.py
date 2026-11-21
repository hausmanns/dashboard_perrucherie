"""Encrypted backup of the finance data (the database + the documents).

The finance data never goes to git. To move it to another machine: export a
`.perrufin` file here, copy it (USB key, cloud drive — it is encrypted), and
import it there with the same passphrase.

Format: b"PERRUFIN1" + 16-byte salt + Fernet token (AES-128-CBC + HMAC-SHA256)
of a zip archive; the key comes from the passphrase through scrypt
(n=2**15, r=8, p=1). A wrong passphrase fails cleanly (HMAC check), nothing
is overwritten.
"""
from __future__ import annotations

import base64
import io
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import db

MAGIC = b"PERRUFIN1"


class BackupError(Exception):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase or "") < 8:
        raise BackupError("phrase de passe trop courte (8 caractères minimum)")
    kdf = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def export(passphrase: str) -> tuple[str, bytes]:
    """(filename, encrypted bytes)."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "finance.db")
        src = sqlite3.connect(db.db_path())
        dst = sqlite3.connect(copy)
        with dst:
            src.backup(dst)          # consistent copy even while the app writes
        src.close()
        dst.close()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copy, "finance.db")
            for f in db.docs_dir().glob("*"):
                if f.is_file():
                    z.write(f, f"docs/{f.name}")
    salt = os.urandom(16)
    token = Fernet(_key(passphrase, salt)).encrypt(buf.getvalue())
    name = f"perrucherie-finances-{datetime.now():%Y%m%d-%H%M}.perrufin"
    return name, MAGIC + salt + token


def restore(blob: bytes, passphrase: str) -> dict:
    if not blob.startswith(MAGIC):
        raise BackupError("ce fichier n'est pas une sauvegarde des finances")
    salt, token = blob[len(MAGIC):len(MAGIC) + 16], blob[len(MAGIC) + 16:]
    try:
        raw = Fernet(_key(passphrase, salt)).decrypt(token)
    except InvalidToken:
        raise BackupError("phrase de passe incorrecte (ou fichier abîmé)")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        if "finance.db" not in names:
            raise BackupError("sauvegarde incomplète")
        for n in names:
            if n.startswith("/") or ".." in n.split("/"):
                raise BackupError("sauvegarde invalide")
        target = db.data_dir()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if db.db_path().exists():
            shutil.copy2(db.db_path(), target / f"finance.before-restore-{stamp}.db")
        with tempfile.TemporaryDirectory() as tmp:
            z.extractall(tmp)
            shutil.copy2(os.path.join(tmp, "finance.db"), db.db_path())
            docs_src = os.path.join(tmp, "docs")
            db.docs_dir().mkdir(parents=True, exist_ok=True)
            n_docs = 0
            if os.path.isdir(docs_src):
                for f in os.listdir(docs_src):
                    shutil.copy2(os.path.join(docs_src, f), db.docs_dir() / f)
                    n_docs += 1
    db.init_db()
    return {"restored": True, "documents": n_docs}
