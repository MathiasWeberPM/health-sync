#!/usr/bin/env python3
"""
Test der offiziellen Garmin Personal API
=======================================

Prüft, ob die neue offizielle API alles liefert, was der bisherige Sync
(inoffizielle Bibliothek) liefert, und was zusätzlich möglich ist.

Gibt NUR Statuscodes, Anzahlen und Ja/Nein aus. Kein Token, kein Key,
keine Gesundheitswerte. Die Ausgabe kann gefahrlos in den Chat kopiert werden.

Key ablegen (einmalig, nicht in den Chat, nicht ins Repo):
    mkdir -p ~/.config/garmin
    nano ~/.config/garmin/connect-personal-api-key     # Key einfügen, speichern
    chmod 600 ~/.config/garmin/connect-personal-api-key

Aufruf:
    cd ~/health-sync && source .venv/bin/activate
    python garmin_personal_test.py
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import requests

BASE = os.environ.get("GARMIN_PERSONAL_API_BASE_URL", "https://personal.connectapi.garmin.com")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
KEY_FILE = Path("~/.config/garmin/connect-personal-api-key").expanduser()

# Referenz-Aktivitäten, bei denen wir die Sonderfelder schon kennen
RIDE_VO2 = 24613971353      # 05.10. VO2max-Fahrt, Garmin-VO2max 40 im FIT
RUN_SSL = 24331988766       # 12.09. Lauf, Step Speed Loss + VO2max 50 im FIT


def load_key() -> str:
    key = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit(f"Kein Key gefunden. Datei anlegen: {KEY_FILE}")
    return key


def get_token(key: str) -> str:
    resp = requests.post(BASE + TOKEN_PATH, headers={"Authorization": f"ApiKey {key}"}, timeout=30)
    print(f"Token-Abruf: HTTP {resp.status_code}")
    if resp.status_code != 200:
        print("  Antwort (gekürzt):", resp.text[:200])
        sys.exit(1)
    data = resp.json()
    token = data.get("access_token") or data.get("accessToken") or data.get("token")
    if not token:
        sys.exit(f"Kein Token im Antwortfeld. Felder: {list(data)}")
    return token


def probe(session: requests.Session, label: str, path: str, params: dict | None = None):
    resp = session.get(BASE + path, params=params, timeout=60)
    size = ""
    if resp.status_code == 200:
        try:
            body = resp.json()
            size = f"{len(body)} Einträge" if isinstance(body, list) else f"Felder: {', '.join(list(body)[:6])}"
        except ValueError:
            size = f"{len(resp.content)} Bytes"
    print(f"  {label:<28} HTTP {resp.status_code}  {size}")
    return resp


def fit_check(session: requests.Session, activity_id: int, label: str):
    resp = session.get(f"{BASE}/download-service/files/activity/{activity_id}", timeout=120)
    if resp.status_code != 200:
        print(f"  {label:<28} HTTP {resp.status_code}")
        return
    try:
        import fitparse
    except ImportError:
        print(f"  {label:<28} OK ({len(resp.content)} Bytes), fitparse fehlt für Detailprüfung")
        return

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = next((n for n in zf.namelist() if n.lower().endswith(".fit")), None)
        if not name:
            print(f"  {label:<28} ZIP ohne FIT: {zf.namelist()}")
            return
        raw = zf.read(name)

    fit = fitparse.FitFile(io.BytesIO(raw))
    vo2 = any(f.name == "unknown_4" for m in fit.get_messages("unknown_140") for f in m)
    session_msg = next(fit.get_messages("session"), None)
    fields = {f.name for f in session_msg} if session_msg else set()
    ssl = "unknown_222" in fields
    power = "avg_power" in fields
    print(f"  {label:<28} OK  FIT {len(raw)//1024} KB | VO2max-Feld: {'ja' if vo2 else 'NEIN'}"
          f" | Step Speed Loss: {'ja' if ssl else 'nein'} | Leistung: {'ja' if power else 'nein'}")


def main() -> None:
    key = load_key()
    token = get_token(key)
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"

    today = date.today()
    week = (today - timedelta(days=7)).isoformat()
    old = date(2025, 1, 1).isoformat()

    print("\n1) Offiziell dokumentierte Endpunkte")
    probe(s, "Aktivitätsliste", "/activitylist-service/activities/search/activities",
          {"start": 0, "limit": 5})
    probe(s, "Schlaf (7 Tage)", "/sleep-service/sleep/dailySleepSummariesByDate",
          {"startDate": week, "endDate": today.isoformat()})
    probe(s, "HRV (7 Tage)", f"/hrv-service/hrv/daily/{week}/{today.isoformat()}")
    probe(s, "Body Battery (7 Tage)", "/wellness-service/wellness/bodyBattery/reports/daily",
          {"startDate": week, "endDate": today.isoformat()})
    probe(s, "Tageszusammenfassungen", "/usersummary-service/v2/dailysummaries",
          {"startDate": week, "endDate": today.isoformat()})
    probe(s, "Fitnessalter", f"/fitnessage-service/fitnessage/v2/{today.isoformat()}")

    print("\n2) Historie: wie weit zurück?")
    probe(s, "Schritte Jan 2025", f"/usersummary-service/stats/steps/daily/{old}/2025-01-28")

    print("\n3) FIT-Dateien: kommen die Sonderfelder mit?")
    fit_check(s, RIDE_VO2, "VO2max-Fahrt 05.10.")
    fit_check(s, RUN_SSL, "Lauf 12.09.")

    print("\n4) Nicht dokumentiert, aber interessant (403/404 = nicht freigegeben)")
    probe(s, "VO2max-Verlauf", f"/metrics-service/metrics/maxmet/daily/{week}/{today.isoformat()}")
    probe(s, "Trainingsstatus", f"/metrics-service/metrics/trainingstatus/aggregated/{today.isoformat()}")
    probe(s, "Trainingsbereitschaft", f"/metrics-service/metrics/trainingreadiness/{today.isoformat()}")
    probe(s, "Wettkampfprognose", "/metrics-service/metrics/racepredictions/latest/me")
    probe(s, "Kraftsätze 06.10.", "/activity-service/activity/24628210106/exerciseSets")

    print("\nFertig. Diese Ausgabe kannst du komplett in den Chat kopieren.")


if __name__ == "__main__":
    main()
