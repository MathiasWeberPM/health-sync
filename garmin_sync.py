#!/usr/bin/env python3
"""
Garmin Connect -> Google Drive
==============================

Laeuft taeglich als GitHub Action. Holt neue Aktivitaeten aus Garmin Connect,
liest die FIT-Dateien aus und schreibt drei CSVs plus ein Rohdatenarchiv in den
Drive-Ordner "Health".

    garmin_activities_<jahr>.csv   eine Zeile pro Einheit
    garmin_laps_<jahr>.csv         eine Zeile pro Runde
    garmin_strength_sets.csv       eine Zeile pro Satz
    fit/<id>.fit                   Originaldatei, unveraendert

Umgebung:
    GOOGLE_OAUTH_TOKEN       JSON mit client_id, client_secret, refresh_token
                             (einmalig erzeugt mit google_oauth_setup.py)
    DRIVE_FOLDER_ID          ID des Ordners "Health"
    GARMIN_TOKENS_JSON       JSON mit dem Inhalt der Garmin-Tokendateien
                             (lokal erzeugt, siehe README-Befehl)

Lokal testen:
    python garmin_sync.py --days 7
    python garmin_sync.py --since 2026-01-01
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import fitparse
from garminconnect import Garmin
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# Grenzen der Herzfrequenzzonen in bpm. Aus den Garmin-Einstellungen
# (HFmax 182). Bei Aenderung der HFmax hier mitziehen.
HR_ZONES = [(0, 108), (108, 126), (126, 144), (144, 162), (162, 300)]

# Garmin legt VO2max in einer undokumentierten Nachricht ab.
VO2MAX_MESSAGE = "unknown_140"
VO2MAX_FIELD = "unknown_4"

# FIT-Standard: exercise_category. Bei Auto-Erkennung raet Garmin bis zu drei
# Kategorien; steht die Uebung im Workout, ist der erste Wert verlaesslich.
CATEGORY = {
    0: "Bankdruecken", 1: "Wadenheben", 2: "Cardio", 3: "Carry", 4: "Chop",
    5: "Core", 6: "Crunch", 7: "Curl", 8: "Kreuzheben", 9: "Flye",
    10: "Hip Raise", 11: "Hip Stability", 12: "Hip Swing", 13: "Hyperextension",
    14: "Seitheben", 15: "Beincurl", 16: "Beinheben", 17: "Ausfallschritt",
    18: "Olympic Lift", 19: "Plank", 20: "Plyo", 21: "Klimmzug",
    22: "Liegestuetz", 23: "Rudern", 24: "Schulterdruecken",
    25: "Shoulder Stability", 26: "Shrug", 27: "Sit-up", 28: "Kniebeuge",
    29: "Ganzkoerper", 30: "Trizepsdruecken", 31: "Warm-up", 32: "Laufen",
    65534: "unbekannt",
}

ACTIVITY_FIELDS = [
    "date", "start", "activity_id", "sport", "sub_sport", "name",
    "duration_min", "distance_km", "elevation_gain_m",
    "avg_hr", "max_hr", "avg_power", "max_power", "normalized_power",
    "intensity_factor", "tss", "avg_cadence",
    "calories", "work_kj",
    "training_effect_aerobic", "training_effect_anaerobic", "vo2max",
    "setting_hr_max", "setting_resting_hr", "setting_ftp",
    "hr_z1_s", "hr_z2_s", "hr_z3_s", "hr_z4_s", "hr_z5_s",
    "fit_file",
]

LAP_FIELDS = [
    "date", "activity_id", "lap_no", "start", "duration_s",
    "avg_power", "max_power", "avg_hr", "max_hr", "avg_cadence",
    "distance_m", "sport",
]

SET_FIELDS = [
    "date", "activity_id", "start", "set_no", "exercise_guess", "alt_guesses",
    "repetitions", "weight_kg", "duration_s", "rest_after_s",
]


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/drive"]


class Drive:
    """
    Schreibt in den Drive-Ordner des Nutzers.

    Bevorzugt OAuth im Namen des eigenen Google-Kontos (GOOGLE_OAUTH_TOKEN).
    Ein Dienstkonto funktioniert nur zum Lesen: neu erzeugte Dateien wuerden dem
    Dienstkonto gehoeren, und das hat keinen eigenen Speicherplatz — Google
    antwortet dann mit storageQuotaExceeded.
    """

    def __init__(self, folder_id: str):
        token = os.environ.get("GOOGLE_OAUTH_TOKEN", "")
        if token:
            data = json.loads(token)
            creds = UserCredentials(
                token=None,
                refresh_token=data["refresh_token"],
                client_id=data["client_id"],
                client_secret=data["client_secret"],
                token_uri="https://oauth2.googleapis.com/token",
                scopes=SCOPES,
            )
        else:
            raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
            if not raw:
                sys.exit("GOOGLE_OAUTH_TOKEN fehlt (oder GOOGLE_SERVICE_ACCOUNT).")
            print("Warnung: Dienstkonto kann keine Dateien anlegen — "
                  "GOOGLE_OAUTH_TOKEN verwenden.")
            creds = service_account.Credentials.from_service_account_info(
                json.loads(raw), scopes=SCOPES
            )
        self.api = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.folder_id = folder_id
        self._fit_folder = None

    def find(self, name: str, parent: str | None = None) -> str | None:
        parent = parent or self.folder_id
        safe = name.replace("'", "\\'")
        result = self.api.files().list(
            q=f"name='{safe}' and '{parent}' in parents and trashed=false",
            fields="files(id)", pageSize=1,
        ).execute()
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def fit_folder(self) -> str:
        if self._fit_folder:
            return self._fit_folder
        found = self.find("fit")
        if not found:
            created = self.api.files().create(
                body={
                    "name": "fit",
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [self.folder_id],
                },
                fields="id",
            ).execute()
            found = created["id"]
        self._fit_folder = found
        return found

    def read_text(self, name: str) -> str:
        file_id = self.find(name)
        if not file_id:
            return ""
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, self.api.files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue().decode("utf-8")

    def write(self, name: str, data: bytes, mime: str, parent: str | None = None) -> None:
        parent = parent or self.folder_id
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        existing = self.find(name, parent)
        if existing:
            self.api.files().update(fileId=existing, media_body=media).execute()
        else:
            self.api.files().create(
                body={"name": name, "parents": [parent]}, media_body=media, fields="id"
            ).execute()


# ---------------------------------------------------------------------------
# FIT-Auswertung
# ---------------------------------------------------------------------------

def first(fit: fitparse.FitFile, name: str) -> dict:
    for message in fit.get_messages(name):
        return {field.name: field.value for field in message}
    return {}


def local_day(value, offset=None) -> str:
    if not value:
        return ""
    moment = value if isinstance(value, datetime) else None
    if moment is None:
        return str(value)[:10]
    if offset and isinstance(offset, str) and len(offset) >= 6 and offset[0] in "+-":
        sign = 1 if offset[0] == "+" else -1
        moment = moment + sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
    return moment.strftime("%Y-%m-%d")


def rnd(value, digits=1):
    return round(value, digits) if isinstance(value, (int, float)) else None


def label(value) -> str:
    return CATEGORY.get(value, f"cat_{value}") if value is not None else ""


def hr_seconds(fit: fitparse.FitFile) -> list[int]:
    buckets = [0] * len(HR_ZONES)
    for message in fit.get_messages("record"):
        hr = None
        for field in message:
            if field.name == "heart_rate":
                hr = field.value
                break
        if hr is None:
            continue
        for index, (low, high) in enumerate(HR_ZONES):
            if low <= hr < high:
                buckets[index] += 1
                break
    return buckets


def vo2max_of(fit: fitparse.FitFile, session: dict):
    """
    VO2max steht in einer undokumentierten Garmin-Nachricht. Dasselbe Feld ist
    bei Einheiten ohne Leistungsmessung mit etwas anderem belegt (Krafttraining
    lieferte dort 26, eine Ausfahrt ohne Powermeter 27). Garmin rechnet den
    Radwert ohnehin nur mit Leistungsdaten — deshalb beides zur Bedingung.
    """
    if not session.get("avg_power"):
        return None
    if session.get("sport") not in ("cycling", "running"):
        return None
    value = first(fit, VO2MAX_MESSAGE).get(VO2MAX_FIELD)
    return value if isinstance(value, (int, float)) and 35 <= value <= 75 else None


def parse_fit(blob: bytes, activity_id: str, name: str):
    fit = fitparse.FitFile(io.BytesIO(blob))

    session = first(fit, "session")
    settings = first(fit, "zones_target")
    profile = first(fit, "user_profile")
    offset = session.get("timezone_offset")
    start = session.get("start_time")
    day = local_day(start, offset)
    sport = session.get("sport") or ""

    zones = hr_seconds(fit)
    duration = session.get("total_timer_time") or 0

    activity = {
        "date": day,
        "start": start.strftime("%H:%M:%S") if isinstance(start, datetime) else "",
        "activity_id": activity_id,
        "sport": sport,
        "sub_sport": session.get("sub_sport") or "",
        "name": name,
        "duration_min": rnd(duration / 60, 1),
        "distance_km": rnd((session.get("total_distance") or 0) / 1000, 2),
        "elevation_gain_m": rnd(session.get("total_ascent"), 0),
        "avg_hr": session.get("avg_heart_rate"),
        "max_hr": session.get("max_heart_rate"),
        "avg_power": session.get("avg_power"),
        "max_power": session.get("max_power"),
        "normalized_power": session.get("normalized_power"),
        "intensity_factor": rnd(session.get("intensity_factor"), 3),
        "tss": rnd(session.get("training_stress_score"), 1),
        "avg_cadence": session.get("avg_cadence"),
        "calories": session.get("total_calories"),
        "work_kj": rnd((session.get("total_work") or 0) / 1000, 0),
        "training_effect_aerobic": rnd(session.get("total_training_effect"), 1),
        "training_effect_anaerobic": rnd(session.get("total_anaerobic_training_effect"), 1),
        "vo2max": vo2max_of(fit, session),
        "setting_hr_max": settings.get("max_heart_rate") or profile.get("default_max_heart_rate"),
        "setting_resting_hr": settings.get("resting_heart_rate") or profile.get("resting_heart_rate"),
        "setting_ftp": settings.get("functional_threshold_power"),
        "hr_z1_s": zones[0], "hr_z2_s": zones[1], "hr_z3_s": zones[2],
        "hr_z4_s": zones[3], "hr_z5_s": zones[4],
        "fit_file": f"{activity_id}.fit",
    }

    laps = []
    for index, message in enumerate(fit.get_messages("lap"), 1):
        lap = {field.name: field.value for field in message}
        lap_start = lap.get("start_time")
        laps.append({
            "date": day,
            "activity_id": activity_id,
            "lap_no": index,
            "start": lap_start.strftime("%H:%M:%S") if isinstance(lap_start, datetime) else "",
            "duration_s": rnd(lap.get("total_timer_time"), 0),
            "avg_power": lap.get("avg_power"),
            "max_power": lap.get("max_power"),
            "avg_hr": lap.get("avg_heart_rate"),
            "max_hr": lap.get("max_heart_rate"),
            "avg_cadence": lap.get("avg_cadence"),
            "distance_m": rnd(lap.get("total_distance"), 0),
            "sport": sport,
        })

    raw_sets = [{f.name: f.value for f in m} for m in fit.get_messages("set")]
    sets, set_no = [], 0
    for index, entry in enumerate(raw_sets):
        if entry.get("set_type") != "active":
            continue
        set_no += 1
        categories = entry.get("category") or ()
        if not isinstance(categories, (list, tuple)):
            categories = (categories,)
        categories = [c for c in categories if c is not None]
        rest = None
        if index + 1 < len(raw_sets) and raw_sets[index + 1].get("set_type") == "rest":
            rest = raw_sets[index + 1].get("duration")
        set_start = entry.get("start_time")
        weight = entry.get("weight")
        sets.append({
            "date": local_day(set_start, offset) or day,
            "activity_id": activity_id,
            "start": set_start.strftime("%H:%M:%S") if isinstance(set_start, datetime) else "",
            "set_no": set_no,
            "exercise_guess": label(categories[0]) if categories else "",
            "alt_guesses": " / ".join(label(c) for c in categories[1:]),
            "repetitions": entry.get("repetitions"),
            "weight_kg": rnd(weight, 2) if weight else "",
            "duration_s": rnd(entry.get("duration"), 0),
            "rest_after_s": rnd(rest, 0),
        })

    return activity, laps, sets


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def merge_csv(drive: Drive, name: str, fields: list, rows: list, key) -> int:
    existing = {}
    text = drive.read_text(name)
    if text:
        for row in csv.DictReader(io.StringIO(text)):
            existing[key(row)] = row

    added = 0
    for row in rows:
        identifier = key(row)
        if identifier not in existing:
            added += 1
            existing[identifier] = {}
        existing[identifier].update(
            {k: ("" if v is None else v) for k, v in row.items()}
        )

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for identifier in sorted(existing, key=str):
        writer.writerow(existing[identifier])
    drive.write(name, buffer.getvalue().encode("utf-8"), "text/csv")
    return added


# ---------------------------------------------------------------------------

def restore_tokens() -> Path:
    """
    Schreibt die Garmin-Tokens aus GARMIN_TOKENS_JSON nach ~/.garth.

    Frueher lief das ueber tar + base64 im Workflow. Das ist an der Shell
    zerbrochen: die Dateien landeten zwar am richtigen Ort, aber mit
    abgeschnittenem Inhalt, und garth scheiterte erst spaeter am JSON.
    Jetzt liegt ein einziges JSON-Objekt im Secret, Python schreibt die
    Dateien selbst. Ohne die Variable bleibt ein vorhandener ~/.garth
    unangetastet - so laeuft es lokal weiter wie bisher.
    """
    token_dir = Path.home() / ".garth"
    raw = os.environ.get("GARMIN_TOKENS_JSON", "").strip()
    if not raw:
        return token_dir

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        sys.exit(f"GARMIN_TOKENS_JSON ist kein gueltiges JSON: {error}")

    token_dir.mkdir(parents=True, exist_ok=True)
    for name, content in payload.items():
        target = token_dir / name
        target.write_text(json.dumps(content), encoding="utf-8")
        target.chmod(0o600)
        print(f"  {name}: {target.stat().st_size} Bytes geschrieben")
    return token_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Garmin-Aktivitaeten nach Drive")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--since", help="Startdatum YYYY-MM-DD")
    parser.add_argument("--force", action="store_true",
                        help="Bereits erfasste Einheiten neu einlesen und ueberschreiben")
    args = parser.parse_args()

    folder_id = os.environ.get("DRIVE_FOLDER_ID", "")
    if not folder_id:
        sys.exit("DRIVE_FOLDER_ID fehlt.")

    start = (datetime.strptime(args.since, "%Y-%m-%d").date() if args.since
             else date.today() - timedelta(days=args.days))
    end = date.today()
    print(f"Zeitraum: {start} bis {end}")

    token_dir = restore_tokens()
    found = sorted(p.name for p in token_dir.glob("*")) if token_dir.is_dir() else []
    print(f"Token-Ordner {token_dir}: {found or 'LEER'}")
    if not found:
        sys.exit("Keine Garmin-Tokens gefunden — Secret GARMIN_TOKENS pruefen.")

    api = Garmin()
    try:
        api.login(str(token_dir))
    except Exception as error:  # noqa: BLE001
        sys.exit(
            f"Garmin-Login fehlgeschlagen: {error}\n"
            "Meist ein Versionskonflikt: die Tokens wurden mit einer anderen "
            "garth-Version erzeugt. Lokal 'garth.save' erneut ausfuehren und das "
            "Secret GARMIN_TOKENS neu setzen."
        )

    drive = Drive(folder_id)

    # Bereits verarbeitete Einheiten aus den vorhandenen CSVs lesen.
    known = set()
    if not args.force:
        for year in {start.year, end.year}:
            text = drive.read_text(f"garmin_activities_{year}.csv")
            for row in csv.DictReader(io.StringIO(text)) if text else []:
                known.add(str(row.get("activity_id")))
    else:
        print("--force: alle Einheiten im Zeitraum werden neu eingelesen")

    listed = api.get_activities_by_date(start.isoformat(), end.isoformat())
    print(f"{len(listed)} Aktivitaeten im Zeitraum, {len(known)} davon bereits erfasst")

    activities, laps, sets = [], [], []
    for item in listed:
        activity_id = str(item.get("activityId"))
        if activity_id in known:
            continue
        name = item.get("activityName") or ""
        print(f"  lade {activity_id} — {name}")
        try:
            blob = api.download_activity(
                activity_id, dl_fmt=api.ActivityDownloadFormat.ORIGINAL
            )
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                fit_names = [n for n in archive.namelist() if n.lower().endswith(".fit")]
                if not fit_names:
                    print("    keine FIT-Datei im Archiv, uebersprungen")
                    continue
                fit_bytes = archive.read(fit_names[0])
        except Exception as error:  # noqa: BLE001
            print(f"    Fehler beim Laden: {error}")
            continue

        try:
            activity, activity_laps, activity_sets = parse_fit(fit_bytes, activity_id, name)
        except Exception as error:  # noqa: BLE001
            print(f"    Fehler beim Auslesen: {error}")
            continue

        activities.append(activity)
        laps.extend(activity_laps)
        sets.extend(activity_sets)
        try:
            drive.write(f"{activity_id}.fit", fit_bytes,
                        "application/octet-stream", drive.fit_folder())
        except Exception as error:  # noqa: BLE001
            print(f"    Archiv-Upload fehlgeschlagen: {error}")

    if not activities:
        print("Nichts Neues.")
        return

    by_year: dict[str, list] = {}
    for activity in activities:
        by_year.setdefault(activity["date"][:4], []).append(activity)
    for year, rows in by_year.items():
        count = merge_csv(drive, f"garmin_activities_{year}.csv", ACTIVITY_FIELDS,
                          rows, lambda r: str(r["activity_id"]))
        print(f"garmin_activities_{year}.csv  (+{count})")

    laps_by_year: dict[str, list] = {}
    for lap in laps:
        laps_by_year.setdefault(lap["date"][:4], []).append(lap)
    for year, rows in laps_by_year.items():
        count = merge_csv(drive, f"garmin_laps_{year}.csv", LAP_FIELDS, rows,
                          lambda r: (str(r["activity_id"]), int(r["lap_no"])))
        print(f"garmin_laps_{year}.csv  (+{count})")

    if sets:
        count = merge_csv(drive, "garmin_strength_sets.csv", SET_FIELDS, sets,
                          lambda r: (str(r["activity_id"]), int(r["set_no"])))
        print(f"garmin_strength_sets.csv  (+{count})")

    print(f"Fertig: {len(activities)} neue Einheiten.")


if __name__ == "__main__":
    main()
