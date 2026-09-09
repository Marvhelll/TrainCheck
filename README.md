# SNCF Tracker — Paris ↔ Nantes

Interface calendrier + détail des trains via l'API interne SNCF Connect, self-hosted sur GitHub Pages avec base SQLite chiffrée.

## Architecture

```
GitHub Actions ──► fetch (Python) ──► SQLite ──► AES-GCM ──► data.sqlite.enc
     cron 6h                                                       │
                                                                   ▼
                                                            git commit + push
                                                                   │
                                                                   ▼
                                                       GitHub Pages (index.html)
                                                                   │
                                        passphrase ──►    sql.js (WASM in browser)
                                                                   │
                                                                   ▼
                                                              interface
```

Aucun serveur. Tout est statique côté hosting, la logique du filtre est côté browser.

## Setup

### 1. Créer le repo GitHub

```bash
cd sncf-tracker
git init
git branch -M main
git remote add origin git@github.com:<toi>/sncf-tracker.git
```

### 2. Activer GitHub Pages

Settings → Pages → **Source: GitHub Actions**.

### 3. Ajouter deux secrets

Settings → Secrets and variables → Actions → **New repository secret** :

- `SNCF_PASSPHRASE` — ta passphrase (min 12 chars, plutôt 20+ aléatoires)
- `SNCF_DATADOME` *(optionnel)* — cookie `datadome` extrait de ton browser (Chrome DevTools → Application → Cookies → sncf-connect.com → datadome). Sert à contourner l'anti-bot depuis les IPs GitHub Actions. À rafraîchir quand ça bloque.

### 4. Premier commit

```bash
git add .
git commit -m "initial import"
git push -u origin main
```

Le workflow tourne, commit `data.sqlite.enc`, puis déploie sur Pages. URL : `https://<toi>.github.io/sncf-tracker/`.

### 5. Test local

```bash
pip install httpx cryptography
export SNCF_PASSPHRASE='ta_passphrase'

# Fetch data (optionnel — utilise le cache existant sinon)
python fetch_calendar.py --days 90
python fetch_full_day.py --days 14

# Build & encrypt
python pack_db.py

# Serve
python -m http.server 8765
# ouvrir http://localhost:8765/index.html
```

## Fichiers

| Fichier | Rôle |
|---|---|
| `fetch_calendar.py` | Prix min/jour sur 91 jours via `/bff/api/v1/calendar/best-prices` |
| `fetch_full_day.py` | Tous les trains du jour via `/bff/api/v1/itineraries`, avec anti-DataDome (retries + reprise) |
| `pack_db.py` | JSON → SQLite → chiffrement AES-GCM |
| `index.html` | UI (unlock screen + sql.js + filtres) |
| `.github/workflows/fetch.yml` | Cron toutes les 6h + deploy Pages |
| `data.json`, `full_days.json` | Cache brut, régénéré à chaque fetch |
| `data.sqlite.enc` | Base chiffrée servie sur Pages |
| `data.sqlite` | **Jamais commit** (ignoré) — base en clair |

## Chiffrement

- **Algorithme** : AES-256-GCM
- **Dérivation clé** : PBKDF2-HMAC-SHA256, 200 000 itérations
- **Salt** : 16 octets aléatoires par build
- **IV** : 12 octets aléatoires par build
- **Format fichier** : `[16 salt][12 IV][ciphertext + 16 tag]`

Côté browser : WebCrypto natif (`crypto.subtle.deriveKey` + `decrypt`). Passphrase jamais transmise, jamais loggée. Optionnellement conservée en `sessionStorage` pour l'onglet courant.

## Sécurité — ce qui est garanti / ce qui ne l'est pas

**Garanti** :
- Sans la passphrase, le `.enc` est illisible (AES-256-GCM avec dérivation coûteuse)
- Une passphrase de 20+ caractères aléatoires est résistante au brute force offline

**Non garanti** :
- Le fichier chiffré est public (repo public) — n'importe qui peut tenter un brute force
- Si tu utilises une passphrase faible (`motdepasse123`), ton fichier tombe en heures

Reco : `openssl rand -base64 24` → colle dans le secret GH + dans un password manager pour toi.

## Anti-bot DataDome

L'endpoint `/itineraries` de SNCF Connect est protégé par [DataDome](https://datadome.co). Depuis les IPs GitHub Actions (largement flag), il faut un cookie `datadome` valide :

1. Ouvrir sncf-connect.com dans Chrome
2. DevTools → Application → Cookies → `sncf-connect.com` → cookie `datadome`
3. Copier la valeur → GitHub Settings → Secrets → `SNCF_DATADOME`

Le cookie tient ~24-72h. Si le workflow échoue en 403 systématique, rafraîchis-le.

L'endpoint `/calendar/best-prices` est moins sensible et passe généralement sans cookie.

## Changer de trajet

Édite `fetch_calendar.py` et `fetch_full_day.py`, remplace `TRIPS = {"pn": ("Paris", "Nantes"), ...}` par tes gares. Les codes sont résolus via autocomplete.

Pour supporter plusieurs trajets, il faut adapter le schéma SQLite (`trips` table avec une colonne `trip_slug`) et l'UI. Pas fait ici pour rester simple.
