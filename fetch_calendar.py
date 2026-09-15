"""
POC — récupère le calendrier des prix min/jour pour Paris <-> Nantes
via l'API interne de SNCF Connect (endpoint /bff/api/v1/calendar/best-prices).

Trouvé en reverse-engineerant le BFF de sncf-connect.com.
Étape 1 : autocomplete pour récupérer un objet Place (id + codes + type).
Étape 2 : POST /bff/api/v1/calendar/best-prices avec {origin, destination, firstDate, lastDate}.

Usage:
    python fetch_calendar.py                 # 60 jours, les 2 sens
    python fetch_calendar.py --days 90
    python fetch_calendar.py --direction pn  # Paris -> Nantes uniquement
    python fetch_calendar.py --direction np  # Nantes -> Paris uniquement
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

BASE = "https://www.sncf-connect.com"

# Clé BFF publique embarquée dans le front sncf-connect.com.
# Elle change potentiellement — extraire à nouveau via DevTools si un jour ça renvoie 401.
BFF_KEY = "ah1MPO-izehIHD-QZZ9y88n-kku876"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Origin": BASE,
    "Referer": f"{BASE}/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "x-bff-key": BFF_KEY,
    "x-api-env": "production",
    "x-app-version": "ffa8ef65d3",
    "x-client-app-id": "front-web",
    "x-client-channel": "web",
    "x-market-locale": "fr_FR",
    "x-device-class": "desktop",
}

TRIPS = {
    "pn": ("Paris", "Nantes"),
    "np": ("Nantes", "Paris"),
}


def resolve_place(client: httpx.Client, term: str) -> dict:
    """Retourne le premier résultat "Ville" ou "Gare" pour un terme donné."""
    r = client.post(
        "/bff/api/v2/autocomplete",
        json={"searchTerm": term, "resultTypes": ["PLACE"]},
    )
    r.raise_for_status()
    data = r.json()
    # On prend la première section "Villes et gares" -> premier résultat (ville)
    for section in data.get("places", []):
        for res in section.get("results", []):
            if res.get("type", {}).get("placeType") in ("CITY", "STATION"):
                return res
    raise RuntimeError(f"Aucun lieu trouvé pour '{term}'")


def fetch_best_prices(client: httpx.Client, origin: dict, destination: dict,
                       first_date: datetime, last_date: datetime) -> list[dict]:
    r = client.post(
        "/bff/api/v1/calendar/best-prices",
        json={
            "origin": origin,
            "destination": destination,
            "firstDate": first_date.isoformat(),
            "lastDate": last_date.isoformat(),
        },
    )
    r.raise_for_status()
    return r.json().get("calendarBestPrices", [])


def summarize(rows: list[dict], label: str) -> None:
    print(f"\n=== {label} ===")
    if not rows:
        print("  (aucune donnée)")
        return
    prices = [(r["date"][:10], r["roundedPrice"]) for r in rows if isinstance(r.get("roundedPrice"), (int, float))]
    if not prices:
        print("  (aucun prix)")
        return
    min_p = min(prices, key=lambda x: x[1])
    max_p = max(prices, key=lambda x: x[1])
    avg = sum(p for _, p in prices) / len(prices)
    print(f"  {len(prices)} jours - min {min_p[1]}€ ({min_p[0]}), max {max_p[1]}€ ({max_p[0]}), moy {avg:.1f}€")
    print()
    for d, p in prices:
        bar = "█" * min(int(p / 2), 30)
        print(f"  {d}  {p:>3}€  {bar}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60,
                        help="Fenêtre en jours (max ~90 côté SNCF).")
    parser.add_argument("--direction", choices=["pn", "np", "both"], default="both")
    parser.add_argument("--out", type=Path, default=Path("data.json"))
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    later = now + timedelta(days=args.days)

    directions = ["pn", "np"] if args.direction == "both" else [args.direction]
    result = {}

    with httpx.Client(base_url=BASE, headers=HEADERS, timeout=30, follow_redirects=True) as client:
        # Warm-up : visite de la homepage pour que DataDome pose ses cookies.
        client.get("/")
        time.sleep(1)

        # Résolution des gares (1 fois)
        places = {}
        for name in {"Paris", "Nantes"}:
            places[name] = resolve_place(client, name)
            print(f"[i] {name} -> {places[name]['label']} ({places[name]['id']})")

        for i, d in enumerate(directions):
            if i > 0:
                time.sleep(3)  # espacer pour éviter DataDome
            o_name, dest_name = TRIPS[d]
            label = f"{o_name} -> {dest_name}"
            try:
                rows = fetch_best_prices(client, places[o_name], places[dest_name], now, later)
            except httpx.HTTPStatusError as e:
                print(f"[!] {label} : HTTP {e.response.status_code}", file=sys.stderr)
                print(f"    body: {e.response.text[:500]}", file=sys.stderr)
                continue

            summarize(rows, label)
            # Normalise en {date, price, time} pour pack_db.py
            result[d] = [
                {"date": r["date"][:10], "price": r.get("roundedPrice"),
                 "time": r["date"][11:16] if r.get("date") else None}
                for r in rows if r.get("roundedPrice") is not None
            ]

    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n→ dump complet écrit dans {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
