"""
Fetch full-day train details for Paris <-> Nantes via SNCF Connect's /itineraries endpoint.

Pour chaque jour, ça récupère TOUS les trains (départ, arrivée, durée, prix, transporteur).
Résultat stocké dans full_days.json — mise à jour progressive, reprend sans doublon.

Usage:
    python fetch_full_day.py --days 30                 # 30 jours à venir, 2 sens
    python fetch_full_day.py --days 14 --dir pn        # Paris → Nantes uniquement
    python fetch_full_day.py --refresh 2026-09-12      # re-fetch une date précise
    python fetch_full_day.py --status                  # afficher couverture actuelle

Anti-bot :
    L'API SNCF Connect est protégée par DataDome. Le script :
    - Espace les appels de 2s minimum
    - Attend 5min si un 403 arrive
    - Skip les dates déjà en cache
    - Peut être relancé plusieurs fois pour compléter

Cookies :
    Pour éviter les blocages, colle un cookie `datadome` extrait de ton navigateur :
    DevTools → Application → Cookies → sncf-connect.com → datadome → copier valeur
    Puis : export SNCF_DATADOME="ta_valeur"
"""

import argparse
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

BASE = "https://www.sncf-connect.com"
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

DATA_FILE = Path(__file__).parent / "full_days.json"

TRIPS = {
    "pn": ("Paris", "Nantes"),
    "np": ("Nantes", "Paris"),
}


def load_state() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"pn": {}, "np": {}, "meta": {"lastFetch": None}}


def save_state(state: dict) -> None:
    state["meta"]["lastFetch"] = datetime.now(timezone.utc).isoformat()
    DATA_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


class DataDomeBlocked(Exception):
    pass


def call(client: httpx.Client, path: str, *, method: str = "GET", body=None) -> dict:
    kwargs = {"method": method, "url": path}
    if body is not None:
        kwargs["json"] = body
    r = client.request(**kwargs)
    text = r.text
    if r.status_code == 403 and text.lstrip().startswith("<"):
        raise DataDomeBlocked(f"403 DataDome sur {path}")
    r.raise_for_status()
    return r.json()


def resolve_place(client: httpx.Client, term: str) -> dict:
    data = call(client, "/bff/api/v2/autocomplete", method="POST",
                body={"searchTerm": term, "resultTypes": ["PLACE"]})
    for section in data.get("places", []):
        for res in section.get("results", []):
            if res.get("type", {}).get("placeType") == "CITY":
                return res
    raise RuntimeError(f"Ville introuvable pour '{term}'")


def get_traveler(client: httpx.Client) -> dict:
    data = call(client, "/bff/api/v1/itineraries/long-distance/options")
    return data["travelerOptions"]["defaultAnonymousTraveler"]


def parse_duration(label: str) -> int | None:
    m = re.match(r"(\d+)h(\d*)", label or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + (int(m.group(2) or 0) if m.group(2) else 0)


def fetch_day_slot(client: httpx.Client, origin: dict, destination: dict,
                    traveler: dict, day_iso: str, start_hour: int) -> list[dict]:
    dt = datetime.fromisoformat(day_iso).replace(hour=start_hour, minute=0, second=0)
    body = {
        "itineraryId": str(uuid.uuid4()),
        "schedule": {"outward": {"date": dt.isoformat(), "arrivalAt": False}},
        "mainJourney": {"origin": origin, "destination": destination},
        "passengers": [traveler],
        "pets": [],
        "trainExpected": True,
        "wishBike": False,
        "strictMode": False,
        "directJourney": False,
        "forceDisplayResults": True,
        "userNavigation": ["IS_NOT_BUSINESS"],
    }
    data = call(client, "/bff/api/v1/itineraries", method="POST", body=body)
    props = (data.get("longDistance") or {}).get("proposals", {}).get("proposals", []) or []
    trains = []
    for p in props:
        travel_id = p.get("travelId") or ""
        trip_day = travel_id[:10] if len(travel_id) >= 10 else None
        if trip_day != day_iso:
            continue  # on ignore les retours d'autres jours dus au style de pagination
        dep = (p.get("departure") or {}).get("timeLabel")
        arr = (p.get("arrival") or {}).get("timeLabel")
        duration = p.get("durationLabel")
        trains.append({
            "dep": dep,
            "arr": arr,
            "dur": duration,
            "durMin": parse_duration(duration),
            "price": (p.get("bestPrice") or {}).get("value"),
            "transporter": p.get("transporterDescription"),
            "direct": (p.get("transporterDescription") or "").lower().startswith("direct"),
            "travelId": travel_id,
        })
    return trains


def merge_trains(existing: list[dict], new: list[dict]) -> list[dict]:
    seen = {(t["dep"], t["dur"], t["price"], t["transporter"]) for t in existing}
    merged = list(existing)
    for t in new:
        key = (t["dep"], t["dur"], t["price"], t["transporter"])
        if key not in seen:
            merged.append(t)
            seen.add(key)
    merged.sort(key=lambda t: t["dep"] or "")
    return merged


def fetch_day(client: httpx.Client, origin: dict, destination: dict,
               traveler: dict, day_iso: str) -> list[dict]:
    all_trains = []
    for start_hour in (4, 12, 18):
        trains = fetch_day_slot(client, origin, destination, traveler, day_iso, start_hour)
        all_trains = merge_trains(all_trains, trains)
        time.sleep(random.uniform(1.5, 2.5))
    return all_trains


def cmd_status(state: dict) -> None:
    for d in ("pn", "np"):
        days = state[d]
        print(f"\n=== {d.upper()} ({len(days)} jours) ===")
        for day_iso in sorted(days.keys())[-15:]:
            trains = days[day_iso]
            if not trains:
                print(f"  {day_iso}  vide")
                continue
            prices = [t["price"] for t in trains if t.get("price")]
            reasonable = [t for t in trains if t.get("dep") and 7 <= int(t["dep"][:2]) <= 21 and (t.get("durMin") or 999) <= 180]
            reasonable_min = min((t["price"] for t in reasonable if t.get("price")), default=None)
            print(f"  {day_iso}  {len(trains):2d} trains · min {min(prices):>4}€  max {max(prices):>4}€"
                  + (f"  · min raisonnable {reasonable_min}€" if reasonable_min else ""))


def prune_past_days(state: dict) -> int:
    """Supprime toutes les dates strictement antérieures à aujourd'hui."""
    today = date.today()
    removed = 0
    for d in ("pn", "np"):
        for day_iso in list(state.get(d, {}).keys()):
            try:
                if date.fromisoformat(day_iso) < today:
                    del state[d][day_iso]
                    removed += 1
            except ValueError:
                pass
    if removed:
        save_state(state)
    return removed


def cmd_fetch(state: dict, days: int, directions: list[str], refresh: str | None) -> None:
    n_removed = prune_past_days(state)
    if n_removed:
        print(f"[i] {n_removed} dates passées purgées du cache")

    datadome_cookie = os.environ.get("SNCF_DATADOME")
    cookies = {}
    if datadome_cookie:
        cookies["datadome"] = datadome_cookie
        print(f"[i] Cookie datadome chargé ({len(datadome_cookie)} chars).")

    with httpx.Client(base_url=BASE, headers=HEADERS, cookies=cookies,
                      timeout=45, follow_redirects=True) as client:
        client.get("/")  # warm up
        time.sleep(1.5)

        try:
            paris = resolve_place(client, "Paris")
            time.sleep(2)
            nantes = resolve_place(client, "Nantes")
            time.sleep(2)
            traveler = get_traveler(client)
        except DataDomeBlocked:
            print("[!] DataDome bloque dès le démarrage. Attends 30 min, ou exporte un cookie datadome frais.", file=sys.stderr)
            return

        places = {"Paris": paris, "Nantes": nantes}
        today = date.today()
        targets = [refresh] if refresh else [(today + timedelta(days=i)).isoformat() for i in range(days)]

        blocked = 0
        for d in directions:
            o_name, dest_name = TRIPS[d]
            print(f"\n=== {o_name} → {dest_name} ===")
            for day_iso in targets:
                if not refresh and day_iso in state[d] and state[d][day_iso]:
                    print(f"  {day_iso}  déjà en cache — skip")
                    continue
                try:
                    trains = fetch_day(client, places[o_name], places[dest_name], traveler, day_iso)
                    state[d][day_iso] = trains
                    save_state(state)
                    if trains:
                        prices = [t["price"] for t in trains if t.get("price")]
                        print(f"  {day_iso}  {len(trains)} trains · {min(prices)}-{max(prices)} €")
                    else:
                        print(f"  {day_iso}  0 trains (weekend/férié ?)")
                    blocked = 0
                except DataDomeBlocked:
                    blocked += 1
                    wait = min(300, 60 * 2 ** blocked)
                    print(f"  {day_iso}  ✖ bloqué DataDome, pause {wait}s ({blocked}× consécutif)")
                    if blocked >= 3:
                        print("[!] 3 blocs consécutifs — arrêt. Relance plus tard.")
                        return
                    time.sleep(wait)
                except httpx.HTTPError as e:
                    print(f"  {day_iso}  ✖ {e}", file=sys.stderr)
                time.sleep(random.uniform(2.5, 4))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--dir", choices=["pn", "np", "both"], default="both")
    parser.add_argument("--refresh", type=str, default=None, help="Force refresh d'une date (YYYY-MM-DD)")
    parser.add_argument("--status", action="store_true", help="Afficher couverture actuelle")
    args = parser.parse_args()

    state = load_state()

    if args.status:
        cmd_status(state)
        return 0

    directions = ["pn", "np"] if args.dir == "both" else [args.dir]
    cmd_fetch(state, args.days, directions, args.refresh)
    print("\n→ Données dans full_days.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
