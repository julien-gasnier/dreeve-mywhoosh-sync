#!/usr/bin/env python3
"""Télécharge les nouvelles activités MyWhoosh dans le dossier watch de Dreeve.

MyWhoosh expose une API non officielle utilisée par son application mobile/web.
Ce script se connecte via l'endpoint services.mywhoosh.com (sans reCAPTCHA),
récupère la liste des activités et télécharge les fichiers FIT originaux pour les
déposer dans ./watch afin que Dreeve (IMPORT_MODE=files) les importe automatiquement.

API non officielle : peut casser si MyWhoosh la modifie.

Utilisation :
    python sync.py              # boucle : sync immédiate puis toutes les N secondes
    python sync.py --once       # une seule synchronisation
    python sync.py --check      # vérifie les identifiants et liste les activités récentes
    python sync.py --once --dry-run
"""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import sqlite3
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import requests
from datetime import timezone

try:
    from zoneinfo import ZoneInfo

    PARIS_TZ = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover - zoneinfo absent
    PARIS_TZ = None

LOGIN_URL = "https://services.mywhoosh.com/http-service/api/login"
ACTIVITIES_URL = "https://service14.mywhoosh.com/v2/rider/profile/activities"
DOWNLOAD_URL = "https://service14.mywhoosh.com/v2/rider/profile/download-activity-file"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

SUPPORTED_EXTENSIONS = (".fit", ".tcx", ".gpx")

_shutdown_requested = False


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


class MyWhooshError(RuntimeError):
    """Erreur fonctionnelle de l'API MyWhoosh."""


class MyWhooshClient:
    def __init__(
        self,
        username: str,
        password: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        whoosh_id: str | None = None,
        device_id: str | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.whoosh_id = whoosh_id
        self.device_id = device_id or str(uuid.uuid4())
        self.correlation_id = str(uuid.uuid4())
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            }
        )
        if access_token:
            self.session.headers["Authorization"] = f"Bearer {access_token}"

    def login(self) -> None:
        if not self.username or not self.password:
            raise MyWhooshError(
                "aucun identifiant : renseigne MYWHOOSH_USERNAME/MYWHOOSH_PASSWORD dans .env"
            )

        payload = {
            "Username": self.username,
            "Password": self.password,
            "Platform": "Android",
            "Action": 1001,
            "CorrelationId": self.correlation_id,
            "DeviceId": self.device_id,
            "Authorization": "",
        }

        response = self.session.post(LOGIN_URL, json=payload, timeout=30)
        try:
            data = response.json()
        except ValueError as exc:
            raise MyWhooshError(
                f"login : réponse non JSON (HTTP {response.status_code})"
            ) from exc

        if not data.get("Success"):
            message = data.get("Message") or data
            raise MyWhooshError(f"login refusé : {message} (HTTP {response.status_code})")

        access_token = data.get("AccessToken")
        if not access_token:
            raise MyWhooshError("login : AccessToken absent de la réponse")

        self.access_token = access_token
        self.refresh_token = data.get("RefreshToken") or self.refresh_token
        self.whoosh_id = data.get("WhooshId") or self.whoosh_id
        self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    def ensure_auth(self) -> None:
        if self.access_token:
            return
        self.login()

    def _authorized_post(self, url: str, payload: dict) -> dict:
        self.ensure_auth()
        response = self.session.post(url, json=payload, timeout=30)
        if response.status_code in (401, 403):
            if not self.username or not self.password:
                raise MyWhooshError("jeton expiré : relance un login ou mets à jour les identifiants")
            self.login()
            response = self.session.post(url, json=payload, timeout=30)

        try:
            data = response.json()
        except ValueError as exc:
            raise MyWhooshError(
                f"{url} : réponse non JSON (HTTP {response.status_code})"
            ) from exc

        if response.status_code >= 400:
            message = data.get("Message") or data.get("message") or data
            raise MyWhooshError(f"{url} : {message} (HTTP {response.status_code})")
        return data

    def list_activities(self, page: int) -> tuple[list[dict], int, int]:
        data = self._authorized_post(ACTIVITIES_URL, {"sortDate": "DESC", "page": page})
        result = data.get("data") or {}
        results = result.get("results") or []
        total_pages = int(result.get("totalPages") or result.get("total_pages") or 1)
        current_page = int(result.get("currentPage") or result.get("current_page") or page)
        return results, total_pages, current_page

    def download_activity(self, activity_file_id: str) -> bytes:
        data = self._authorized_post(DOWNLOAD_URL, {"fileId": activity_file_id})
        download_url = data.get("data") or data.get("url")
        if not isinstance(download_url, str) or not download_url:
            raise MyWhooshError(f"activité {activity_file_id} : URL de téléchargement absente")

        response = requests.get(download_url, headers={"User-Agent": USER_AGENT}, timeout=120)
        if response.status_code >= 400:
            response = self.session.get(download_url, timeout=120)
        response.raise_for_status()
        return response.content


def extract_activity_file(content: bytes) -> tuple[bytes, str]:
    if not content:
        raise MyWhooshError("fichier vide")

    if content[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for member in archive.namelist():
                extension = Path(member).suffix.lower()
                if extension in SUPPORTED_EXTENSIONS:
                    return archive.read(member), extension
        raise MyWhooshError("archive ZIP sans fichier .fit/.tcx/.gpx")

    if len(content) > 12 and content[8:12] == b".FIT":
        return content, ".fit"

    # Certains GPX MyWhoosh commencent par un BOM UTF-8 (\xef\xbb\xbf).
    xml_content = content[3:] if content.startswith(b"\xef\xbb\xbf") else content
    head = xml_content[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<gpx") or head.startswith(b"<TrainingCenterDatabase"):
        return xml_content, ".gpx" if b"<gpx" in head else ".tcx"

    preview = head[:120].decode("utf-8", errors="replace")
    raise MyWhooshError(f"contenu non reconnu (début : {preview!r})")


def load_state(state_dir: Path) -> dict:
    state_file = state_dir / "synced.json"
    if not state_file.exists():
        return {"version": 1, "activities": {}}
    try:
        with state_file.open(encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state.get("activities"), dict):
            state["activities"] = {}
        return state
    except (OSError, ValueError) as exc:
        log(f"état illisible, on repart de zéro : {exc}")
        return {"version": 1, "activities": {}}


def load_auth(state_dir: Path) -> dict:
    auth_file = state_dir / "auth.json"
    if not auth_file.exists():
        return {}
    try:
        with auth_file.open(encoding="utf-8") as handle:
            auth = json.load(handle)
        if isinstance(auth, dict):
            return auth
    except (OSError, ValueError):
        pass
    return {}


def save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "synced.json"
    temporary_file = state_file.with_suffix(".json.tmp")
    with temporary_file.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_file, state_file)


def save_auth(state_dir: Path, auth: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    auth_file = state_dir / "auth.json"
    temporary_file = auth_file.with_suffix(".json.tmp")
    with temporary_file.open("w", encoding="utf-8") as handle:
        json.dump(auth, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_file, auth_file)
    try:
        os.chmod(auth_file, 0o600)
    except OSError:
        pass


def activity_already_in_watch(watch_dir: Path, activity_file_id: str) -> bool:
    return any(
        (watch_dir / f"mywhoosh_{activity_file_id}{extension}").exists()
        for extension in SUPPORTED_EXTENSIONS
    )


def ignored_activity_file_ids() -> set[str]:
    raw = os.getenv("MYWHOOSH_IGNORED_ACTIVITY_FILE_IDS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def get_dreeve_activities() -> tuple[set[str], list[tuple[datetime, float]]]:
    """Retourne les clés des fichiers mywhoosh_* déjà importés et les activités Dreeve.

    Les activités Dreeve sont renvoyées sous forme (startDateTime naïf, distance en km).
    Elles servent à détecter les doublons même quand l'heure diffère (anciens imports
    Strava en UTC vs FIT en heure locale).
    """
    db_path = os.getenv("DREEVE_DB") or "/db/strava.db"
    keys: set[str] = set()
    activities: list[tuple[datetime, float]] = []
    if not os.path.exists(db_path):
        return keys, activities
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "SELECT fi.originalFilename FROM FileImport fi "
            "JOIN Activity a ON a.activityId = fi.activityId "
            "WHERE lower(fi.originalFilename) LIKE 'mywhoosh_%'"
        )
        for (filename,) in cur.fetchall():
            filename = filename or ""
            if filename.startswith("mywhoosh_"):
                name = filename[len("mywhoosh_") :]
                if "." in name:
                    name = name.rsplit(".", 1)[0]
                if name:
                    keys.add(name)

        cur.execute(
            "SELECT startDateTime, distance FROM Activity "
            "WHERE startDateTime IS NOT NULL AND distance IS NOT NULL"
        )
        for start, distance in cur.fetchall():
            try:
                parsed = datetime.fromisoformat(str(start))
                activities.append((parsed, float(distance) / 1000.0))
            except (TypeError, ValueError):
                continue
        conn.close()
    except Exception as exc:
        log(f"lecture Dreeve DB impossible : {exc}")
    return keys, activities


def get_mywhoosh_start(row: dict) -> datetime | None:
    raw = row.get("startDatetime") or row.get("start_datetime")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    epoch = row.get("date")
    if epoch is not None:
        try:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    return None


def is_already_in_dreeve(
    row: dict,
    dreeve_activities: list[tuple[datetime, float]],
    distance_tolerance_km: float = 0.05,
) -> bool:
    """Détecte un doublon même avec un décalage UTC/local (jusqu'à 3 h)."""
    start = get_mywhoosh_start(row)
    if start is None:
        return False
    distance = row.get("distance")
    try:
        distance_km = float(distance) if distance is not None else None
    except (TypeError, ValueError):
        distance_km = None
    if distance_km is not None and distance_km <= 0:
        distance_km = None

    start_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
    start_local = (
        start.astimezone(PARIS_TZ).replace(tzinfo=None) if PARIS_TZ else start_utc
    )

    for existing_start, existing_km in dreeve_activities:
        time_offset = min(
            abs((existing_start - start_utc).total_seconds()),
            abs((existing_start - start_local).total_seconds()),
        )
        same_distance = (
            distance_km is not None
            and abs(existing_km - distance_km) <= distance_tolerance_km
        )
        if time_offset <= 600 and same_distance:
            return True
        # Ancien import Strava : heure UTC stockée au lieu de l'heure locale.
        if same_distance and time_offset <= 3 * 3600:
            return True
    return False


def write_activity(watch_dir: Path, activity_file_id: str, content: bytes, extension: str) -> Path:
    watch_dir.mkdir(parents=True, exist_ok=True)
    destination = watch_dir / f"mywhoosh_{activity_file_id}{extension}"
    temporary = watch_dir / f".mywhoosh_{activity_file_id}{extension}.part"
    temporary.write_bytes(content)
    os.replace(temporary, destination)
    return destination


def get_activity_key(row: dict) -> str | None:
    for key in ("activityFileId", "activity_file_id", "fileId", "file_id"):
        value = row.get(key)
        if value:
            return str(value)
    for key in ("id", "activityId"):
        value = row.get(key)
        if value:
            return str(value)
    return None


def sync_once(client: MyWhooshClient, options: argparse.Namespace, state_dir: Path) -> int:
    watch_dir = Path(options.watch_dir)
    state = load_state(state_dir)
    known_activities = state["activities"]

    downloaded = 0
    skipped = 0
    ignored = 0
    failed = 0
    scanned = 0
    ignored_ids = ignored_activity_file_ids()
    dreeve_keys, dreeve_activities = get_dreeve_activities()

    max_pages = options.max_pages
    if max_pages <= 0:
        max_pages = 10**9

    page = 1
    while page <= max_pages:
        try:
            rows, total_pages, _ = client.list_activities(page)
        except MyWhooshError as exc:
            log(f"ERREUR liste page {page} : {exc}")
            break

        if not rows:
            break

        for row in rows:
            activity_key = get_activity_key(row)
            if not activity_key:
                log("activité sans identifiant, ignorée")
                continue

            scanned += 1
            if (
                activity_key in ignored_ids
                or activity_key in dreeve_keys
                or is_already_in_dreeve(row, dreeve_activities)
            ):
                ignored += 1
                continue

            already_synced = activity_key in known_activities and not options.resync
            if already_synced or (
                activity_already_in_watch(watch_dir, activity_key) and not options.resync
            ):
                skipped += 1
                continue

            label = row.get("title") or row.get("name") or row.get("startDatetime") or activity_key
            if options.dry_run:
                log(f"[dry-run] à télécharger : {activity_key} ({label})")
                downloaded += 1
                continue

            try:
                content = client.download_activity(activity_key)
                content, extension = extract_activity_file(content)
                destination = write_activity(watch_dir, activity_key, content, extension)
                known_activities[activity_key] = {
                    "file": destination.name,
                    "syncedAt": datetime.now().isoformat(timespec="seconds"),
                    "label": str(label),
                }
                save_state(state_dir, state)
                downloaded += 1
                log(f"téléchargé : {destination.name} ({label})")
            except Exception as exc:
                failed += 1
                log(f"échec activité {activity_key} : {exc}")

            time.sleep(0.3)

        if total_pages and page >= total_pages:
            break
        page += 1

    log(
        f"terminé : {scanned} parcourues, {downloaded} téléchargées, "
        f"{skipped} déjà présentes, {ignored} ignorées, {failed} en échec"
    )
    return 1 if failed and not downloaded else 0


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronise les activités MyWhoosh vers le dossier watch de Dreeve."
    )
    parser.add_argument("--once", action="store_true", help="une seule synchronisation puis sortie")
    parser.add_argument("--check", action="store_true", help="vérifie le login et liste les activités récentes")
    parser.add_argument("--dry-run", action="store_true", help="liste ce qui serait téléchargé, sans écrire")
    parser.add_argument("--resync", action="store_true", help="ignore l'état local et retélécharge tout")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.getenv("MYWHOOSH_MAX_PAGES", os.getenv("MAX_PAGES", "0"))),
        help="nombre de pages à parcourir (0 = toutes, défaut : 0)",
    )
    parser.add_argument("--watch-dir", default=os.getenv("WATCH_DIR", "/watch"))
    parser.add_argument("--state-dir", default=os.getenv("STATE_DIR", "/state"))
    return parser.parse_args(argv)


def build_client(options: argparse.Namespace) -> tuple[MyWhooshClient, dict]:
    state_dir = Path(options.state_dir)
    auth = load_auth(state_dir)
    username = os.getenv("MYWHOOSH_USERNAME", "").strip() or os.getenv("MYWHOOSH_EMAIL", "").strip()
    password = os.getenv("MYWHOOSH_PASSWORD", "").strip()
    access_token = auth.get("access_token") or auth.get("AccessToken") or os.getenv("MYWHOOSH_ACCESS_TOKEN", "").strip()
    refresh_token = auth.get("refresh_token") or auth.get("RefreshToken")
    whoosh_id = auth.get("whoosh_id") or auth.get("WhooshId")
    device_id = auth.get("device_id")

    if not access_token and (not username or not password):
        log(
            "MYWHOOSH_USERNAME/MYWHOOSH_PASSWORD ou MYWHOOSH_ACCESS_TOKEN "
            "doivent être définis dans .env"
        )
        raise SystemExit(2)

    client = MyWhooshClient(
        username=username,
        password=password,
        access_token=access_token or None,
        refresh_token=refresh_token or None,
        whoosh_id=whoosh_id or None,
        device_id=device_id or None,
    )
    return client, auth


def handle_signal(signum, _frame) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    log(f"signal {signum} reçu, arrêt après le cycle en cours")


def get_activity_key(row: dict) -> str | None:
    for key in ("activityFileId", "activity_file_id", "fileId", "file_id"):
        value = row.get(key)
        if value:
            return str(value)
    for key in ("id", "activityId"):
        value = row.get(key)
        if value:
            return str(value)
    return None


def main(argv: list[str]) -> int:
    options = parse_arguments(argv)
    state_dir = Path(options.state_dir)
    client, auth = build_client(options)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        if client.access_token and not client.username:
            log("connecté à MyWhoosh via jeton enregistré")
        else:
            client.ensure_auth()
            log("connecté à MyWhoosh")

        save_auth(
            state_dir,
            {
                "access_token": client.access_token,
                "refresh_token": client.refresh_token,
                "whoosh_id": client.whoosh_id,
                "device_id": client.device_id,
            },
        )

        if options.check:
            rows, total_pages, current_page = client.list_activities(1)
            log(f"{len(rows)} activité(s) sur la page {current_page}, {total_pages} page(s) au total")
            for row in rows[:10]:
                key = get_activity_key(row) or "?"
                label = row.get("title") or row.get("name") or row.get("startDatetime") or "sans titre"
                log(f"  - {key} | {row.get('startDatetime') or row.get('date') or '?'} | {label}")
            return 0

        if options.once or options.dry_run:
            return sync_once(client, options, state_dir)
    except MyWhooshError as exc:
        log(f"ERREUR : {exc}")
        return 1

    interval = int(os.getenv("MYWHOOSH_SYNC_INTERVAL_SECONDS", os.getenv("SYNC_INTERVAL_SECONDS", "900")))
    log(f"démarrage de la boucle de synchronisation (toutes les {interval}s)")
    while not _shutdown_requested:
        try:
            sync_once(client, options, state_dir)
        except Exception as exc:
            log(f"ERREUR pendant la synchronisation : {exc}")

        for _ in range(interval):
            if _shutdown_requested:
                break
            time.sleep(1)

    log("arrêt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
