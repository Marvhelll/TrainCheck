"""
Construit et chiffre la base SQLite pour publication sur GitHub Pages.

Lit :
    full_days.json   — détails de trains via /itineraries
    data.json        — prix min/jour via /calendar/best-prices (facultatif)

Écrit :
    data.sqlite      — base SQLite en clair (pour debug local)
    data.sqlite.enc  — même base chiffrée AES-GCM avec ta passphrase

Format du fichier chiffré :
    [16 octets salt][12 octets IV][ciphertext + 16 octets tag GCM]

Dérivation clé : PBKDF2-SHA256, 200 000 itérations.
Compatible directement avec WebCrypto côté browser.

Usage :
    python pack_db.py                                   # utilise $SNCF_PASSPHRASE
    SNCF_PASSPHRASE='ma_phrase_secrete' python pack_db.py
"""

import json
import os
import secrets
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

HERE = Path(__file__).parent
SQLITE_PATH = HERE / "data.sqlite"
ENCRYPTED_PATH = HERE / "data.sqlite.enc"
PBKDF2_ITERATIONS = 200_000  # aligné avec le browser


def build_db() -> None:
    if SQLITE_PATH.exists():
        SQLITE_PATH.unlink()
    conn = sqlite3.connect(SQLITE_PATH)
    conn.executescript("""
        CREATE TABLE trains (
            fetched_at   TEXT NOT NULL,     -- horodatage ISO du snapshot
            direction    TEXT NOT NULL,     -- 'pn' ou 'np'
            travel_day   TEXT NOT NULL,     -- YYYY-MM-DD
            dep          TEXT,              -- 'HH:MM'
            dur          TEXT,              -- '2h15'
            dur_min      INTEGER,
            price        REAL,
            transporter  TEXT,
            direct       INTEGER            -- 0/1
        );
        CREATE INDEX idx_trains_lookup ON trains (direction, travel_day);
        CREATE INDEX idx_trains_time ON trains (fetched_at);

        CREATE TABLE best_prices (
            fetched_at   TEXT NOT NULL,
            direction    TEXT NOT NULL,
            travel_day   TEXT NOT NULL,
            price        REAL,
            dep          TEXT
        );
        CREATE INDEX idx_best_lookup ON best_prices (direction, travel_day);

        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = date.today()

    def is_future_or_today(day_iso: str) -> bool:
        try:
            return date.fromisoformat(day_iso[:10]) >= today
        except (ValueError, TypeError):
            return False

    # /itineraries
    full = HERE / "full_days.json"
    if full.exists():
        data = json.loads(full.read_text())
        rows = []
        skipped_past = 0
        for direction, days in data.items():
            if direction not in ("pn", "np"):
                continue
            for day, trains in days.items():
                if not is_future_or_today(day):
                    skipped_past += 1
                    continue
                for t in trains:
                    if t.get("price") is None:
                        continue
                    rows.append((
                        fetched_at, direction, day,
                        t.get("dep"), t.get("dur"), t.get("durMin"),
                        t.get("price"), t.get("t"),
                        1 if (t.get("t") or "").lower().startswith("direct") else 0,
                    ))
        conn.executemany(
            "INSERT INTO trains (fetched_at, direction, travel_day, dep, dur, dur_min, price, transporter, direct) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        print(f"[i] {len(rows)} trains insérés depuis full_days.json ({skipped_past} jours passés ignorés)")

    # /calendar/best-prices
    cal = HERE / "data.json"
    if cal.exists():
        data = json.loads(cal.read_text())
        rows = []
        skipped_past = 0
        for direction, days in data.items():
            if direction not in ("pn", "np"):
                continue
            for r in days:
                day = r.get("date", "")
                if not is_future_or_today(day):
                    skipped_past += 1
                    continue
                rows.append((
                    fetched_at, direction, day,
                    r.get("price"), r.get("time"),
                ))
        conn.executemany(
            "INSERT INTO best_prices (fetched_at, direction, travel_day, price, dep) VALUES (?,?,?,?,?)", rows)
        print(f"[i] {len(rows)} prix min insérés depuis data.json ({skipped_past} jours passés ignorés)")

    conn.execute("INSERT INTO meta (key, value) VALUES ('generated_at', ?)", (fetched_at,))
    conn.commit()
    conn.close()
    print(f"[i] SQLite écrit : {SQLITE_PATH} ({SQLITE_PATH.stat().st_size} octets)")


def encrypt_db(passphrase: str) -> None:
    plaintext = SQLITE_PATH.read_bytes()
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITERATIONS)
    key = kdf.derive(passphrase.encode())
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    ENCRYPTED_PATH.write_bytes(salt + iv + ciphertext)
    print(f"[i] Chiffré : {ENCRYPTED_PATH} ({ENCRYPTED_PATH.stat().st_size} octets)")


def main() -> int:
    passphrase = os.environ.get("SNCF_PASSPHRASE")
    if not passphrase:
        print("[!] Passphrase manquante — export SNCF_PASSPHRASE='...'", file=sys.stderr)
        return 1
    if len(passphrase) < 12:
        print("[!] Passphrase trop courte (min 12 caractères recommandé).", file=sys.stderr)
        return 1
    build_db()
    encrypt_db(passphrase)
    return 0


if __name__ == "__main__":
    sys.exit(main())
