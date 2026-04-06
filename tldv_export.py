#!/usr/bin/env python3
"""
tl;dv Full Export Script
========================
Exports all meeting transcripts and notes (JSON) from your tl;dv account,
and downloads video/audio recordings.

Requirements:
    pip install requests
    ffmpeg (installed and available in PATH - for audio extraction)

Usage:
    python tldv_export.py                        # export transcripts and notes
    python tldv_export.py --dry-run              # validate config without running the export
    python tldv_export.py --with-audio           # export transcripts/notes and also download audio
    python tldv_export.py --with-audio --with-video  # transcripts/notes + audio + video
    python tldv_export.py --generate-exclude     # build exclude_meetings.txt from local folders
    python tldv_export.py --update-metadata      # rebuild all_meetings.json from local folders
    python tldv_export.py --from 2024-03-07      # meetings on or after this date
    python tldv_export.py --to 2025-09-15        # meetings on or before this date
    python tldv_export.py --from 2024-03-07 --to 2025-09-15  # specific date range

    python tldv_export.py --with-audio --only-existing  # add audio for already exported meetings
    python tldv_export.py --with-audio --workers 3      # parallel downloads (1-8, default: 4)

    Flags can be combined:
    python tldv_export.py --with-audio --from 2025-01-01
    python tldv_export.py --with-audio --with-video --from 2026-03-06
    python tldv_export.py --with-video --from 2024-03-07 --to 2025-09-15

    To reprocess meetings that previously failed, create a retry_meetings.txt file
    in the same folder and paste either log lines or meeting IDs into it.

    To exclude meetings that were already exported (for example, to share the list
    with teammates), run --generate-exclude and send them the generated file.

Configuration:
    Set the environment variable below, or create a .env file:
        TLDV_API_KEY=your_api_key_here

    To get the API key:
        Go to https://tldv.io/app/settings/personal-settings/api-keys
"""

import os
import re
import sys
import json
import time
import shutil
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

try:
    import requests
except ImportError:
    print("❌ 'requests' is not installed. Run: pip install requests")
    sys.exit(1)


# ─── Configuration ───────────────────────────────────────────────────────────

API_BASE = "https://pasta.tldv.io/v1alpha1"

OUTPUT_DIR = Path("tldv_export")
MEETINGS_DIR = OUTPUT_DIR / "meetings"
METADATA_DIR = OUTPUT_DIR / "meetings_metadata"

RETRY_FILE = "retry_meetings.txt"
EXCLUDE_FILE = "exclude_meetings.txt"

REQUEST_DELAY = 0.5
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
DOWNLOAD_RETRY_ATTEMPTS = 3
DOWNLOAD_CHUNK_SIZE = 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Argument Parsing ────────────────────────────────────────────────────────

def parse_args():
    """Parse the small CLI surface without introducing argparse as a dependency."""
    known_flags = {
        "--dry-run",
        "--dryrun",
        "--with-audio",
        "--with-video",
        "--generate-exclude",
        "--update-metadata",
        "--only-existing",
        "--from",
        "--to",
        "--workers",
    }
    for arg in sys.argv[1:]:
        if arg.startswith("--") and arg not in known_flags:
            print(f"❌ Unknown flag: {arg}")
            sys.exit(1)

    args = {
        "dry_run": "--dry-run" in sys.argv or "--dryrun" in sys.argv,
        "with_audio": "--with-audio" in sys.argv,
        "with_video": "--with-video" in sys.argv,
        "gen_exclude": "--generate-exclude" in sys.argv,
        "update_metadata": "--update-metadata" in sys.argv,
        "only_existing": "--only-existing" in sys.argv,
        "workers": DEFAULT_WORKERS,
        "date_from": None,
        "date_to": None,
    }

    # Keep argument parsing simple and dependency-free because this is a single-file script.
    for i, arg in enumerate(sys.argv):
        if arg == "--from" and i + 1 < len(sys.argv):
            try:
                args["date_from"] = datetime.strptime(sys.argv[i + 1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                print(f"❌ Invalid date for --from: '{sys.argv[i + 1]}'. Use YYYY-MM-DD.")
                sys.exit(1)
        if arg == "--to" and i + 1 < len(sys.argv):
            try:
                args["date_to"] = datetime.strptime(sys.argv[i + 1], "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
            except ValueError:
                print(f"❌ Invalid date for --to: '{sys.argv[i + 1]}'. Use YYYY-MM-DD.")
                sys.exit(1)
        if arg == "--workers" and i + 1 < len(sys.argv):
            try:
                w = int(sys.argv[i + 1])
                args["workers"] = max(1, min(w, MAX_WORKERS))
            except ValueError:
                print(f"❌ Invalid value for --workers: '{sys.argv[i + 1]}'. Use a number from 1 to {MAX_WORKERS}.")
                sys.exit(1)

    return args


def resolve_export_plan(args: dict) -> dict:
    """Translate CLI flags into the concrete set of outputs to generate."""
    return {
        "text_exports": True,
        "audio": args["with_audio"],
        "video": args["with_video"],
    }


def describe_export_plan(plan: dict) -> str:
    """Return a human-readable summary of the selected outputs."""
    outputs = []
    if plan["text_exports"]:
        outputs.extend(["transcripts", "notes"])
    if plan["audio"]:
        outputs.append("audio")
    if plan["video"]:
        outputs.append("video")
    return ", ".join(outputs)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_config():
    """Load the API key from env files first, then fall back to interactive input."""
    script_dir = Path(__file__).resolve().parent
    search_dirs = [Path.cwd(), script_dir]
    env_names = [".env", "env_template.env", "env_template.txt"]

    found = False
    for directory in search_dirs:
        for env_name in env_names:
            env_path = directory / env_name
            if env_path.exists():
                log.info(f"  Loading configuration from: {env_path}")
                # Do not overwrite real environment variables with local file defaults.
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        value = value.strip().strip("'\"")
                        os.environ.setdefault(key.strip(), value)
                found = True
                break
        if found:
            break

    if not found:
        log.warning("  No .env-style file found.")

    api_key = os.environ.get("TLDV_API_KEY", "").strip()
    # Support both the original Portuguese placeholders and English template variants.
    placeholders = [
        "cole_sua_api_key_aqui",
        "paste_your_api_key_here",
        "",
    ]
    if api_key in placeholders:
        api_key = ""

    if not api_key:
        print("\n⚠️  TLDV_API_KEY not found.")
        print("   Set it as an environment variable or in env_template.env")
        print("   Generate your key at: https://tldv.io/app/settings/personal-settings/api-keys\n")
        api_key = input("Or paste your API key here: ").strip()
        if not api_key:
            print("❌ API key is required. Exiting.")
            sys.exit(1)

    return api_key


def sanitize_filename(name: str) -> str:
    """Make folder/file names safe for the local filesystem."""
    unsafe = '<>:"/\\|?*'
    for ch in unsafe:
        name = name.replace(ch, "_")
    return name.strip().strip(".")[:200]


def extract_date_str(happened_at: str) -> str:
    """Extract YYYY-MM-DD from either ISO or JS Date format.

    Handles:
        '2024-04-05T18:44:00Z'                                          → '2024-04-05'
        'Fri Apr 05 2024 18:44:00 GMT+0000 (Coordinated Universal Time)' → '2024-04-05'
    """
    if not happened_at:
        return ""

    # Already ISO? First 10 chars are YYYY-MM-DD
    if re.match(r'^\d{4}-\d{2}-\d{2}', happened_at):
        return happened_at[:10]

    # JS Date.toString() format: "Day Mon DD YYYY HH:MM:SS GMT..."
    m = re.match(r'\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})', happened_at)
    if m:
        months = {
            "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
            "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
            "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
        }
        month = months.get(m.group(1), "01")
        day = m.group(2).zfill(2)
        year = m.group(3)
        return f"{year}-{month}-{day}"

    return happened_at[:10]


def parse_meeting_datetime(happened_at: str) -> Optional[datetime]:
    """Parse happenedAt to datetime, handling both ISO and JS Date formats."""
    if not happened_at:
        return None

    # ISO format
    if re.match(r'^\d{4}-\d{2}-\d{2}', happened_at):
        try:
            return datetime.fromisoformat(happened_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    # JS Date format: "Fri Apr 05 2024 18:44:00 GMT+0000 (...)"
    m = re.match(r'\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})', happened_at)
    if m:
        months = {
            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
            "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
        }
        try:
            return datetime(
                int(m.group(3)), months.get(m.group(1), 1), int(m.group(2)),
                int(m.group(4)), int(m.group(5)), int(m.group(6)),
                tzinfo=timezone.utc,
            )
        except (ValueError, TypeError):
            return None

    return None


def meeting_folder_name(meeting: dict) -> str:
    """Generate standardized folder name: YYYY-MM-DD_Meeting Name."""
    # Keep the fallback stable so older exports still map to the same folder names.
    name = meeting.get("name", "sem_nome")
    date = extract_date_str(meeting.get("happenedAt", ""))
    return sanitize_filename(f"{date}_{name}")


def safe_json_dump(data, filepath: Path):
    """Write JSON with predictable formatting and ensure the parent folder exists."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def filter_by_date(meetings: list, date_from, date_to) -> list:
    """Return only meetings that fall inside the optional UTC date range."""
    if not date_from and not date_to:
        return meetings
    filtered = []
    for m in meetings:
        dt = parse_meeting_datetime(m.get("happenedAt", ""))
        if not dt:
            continue
        if date_from and dt < date_from:
            continue
        if date_to and dt > date_to:
            continue
        filtered.append(m)
    return filtered


# ─── List File Loaders ───────────────────────────────────────────────────────

def _load_ids_from_file(filename: str, label: str) -> Optional[set]:
    """Load meeting IDs from a helper file, accepting raw IDs or copied log lines."""
    script_dir = Path(__file__).resolve().parent
    for directory in [Path.cwd(), script_dir]:
        path = directory / filename
        if path.exists():
            log.info(f"{'📋' if 'retry' in label.lower() else '🚫'} {label}: {path}")
            ids = set()
            id_pattern = re.compile(r'[a-f0-9]{24}')
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                ids.update(id_pattern.findall(line))
            log.info(f"   {len(ids)} meetings\n")
            return ids if ids else None
    return None


def load_retry_ids() -> Optional[set]:
    return _load_ids_from_file(RETRY_FILE, "Retry file found")


def load_exclude_ids() -> set:
    return _load_ids_from_file(EXCLUDE_FILE, "Exclude file found") or set()


# ─── Standalone Commands ─────────────────────────────────────────────────────

def _scan_local_meetings():
    """Scan local export folders and return lightweight metadata dicts."""
    if not MEETINGS_DIR.exists():
        log.error(f"❌ Folder '{MEETINGS_DIR}' not found. Run an export first.")
        sys.exit(1)

    entries = []
    for meeting_dir in sorted(MEETINGS_DIR.iterdir()):
        if not meeting_dir.is_dir():
            continue

        meeting_id, meeting_name, meeting_date, happened_at = None, meeting_dir.name, "", ""

        # transcript.json and notes.json both embed enough metadata to rebuild indexes later.
        for filename in ["transcript.json", "notes.json"]:
            json_path = meeting_dir / filename
            if json_path.exists():
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    meta = data.get("_meeting_metadata", {})
                    mid = meta.get("id") or data.get("meetingId")
                    if mid:
                        meeting_id = mid
                        meeting_name = meta.get("name", meeting_dir.name)
                        happened_at = meta.get("happenedAt", "")
                        meeting_date = extract_date_str(happened_at)
                        break
                except (json.JSONDecodeError, IOError):
                    continue

        entries.append({
            "id": meeting_id,
            "name": meeting_name,
            "date": meeting_date,
            "happenedAt": happened_at,
            "dir": meeting_dir,
        })

    return entries


def generate_exclude_list():
    """Build a reusable exclude list from the meetings already present on disk."""
    entries = _scan_local_meetings()
    valid = [(e["id"], e["date"], e["name"]) for e in entries if e["id"]]

    if not valid:
        log.error("No meetings with an ID were found.")
        sys.exit(1)

    output_path = Path(EXCLUDE_FILE)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# Exclude list generated at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")
        f.write(f"# {len(valid)} meetings already exported\n")
        f.write(f"# Share this file with teammates so they can avoid duplicate downloads.\n\n")
        for mid, date, name in valid:
            f.write(f"{mid}  # {date} — {name}\n")

    print(f"\n✅ {EXCLUDE_FILE} generated with {len(valid)} meetings")
    print(f"   Path: {output_path.resolve()}")


def update_metadata_from_local():
    """Rebuild the global metadata file from whatever was already exported locally."""
    entries = _scan_local_meetings()

    all_meta = []
    for e in entries:
        meta = {}
        # Prefer the first file that still carries the embedded API metadata payload.
        for filename in ["transcript.json", "notes.json"]:
            json_path = e["dir"] / filename
            if json_path.exists():
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    m = data.get("_meeting_metadata", {})
                    if m.get("id"):
                        meta = dict(m)
                        break
                except (json.JSONDecodeError, IOError):
                    continue

        if not meta:
            meta = {"id": None, "name": e["name"]}

        meta["_local"] = {
            "folder": e["dir"].name,
            "has_transcript": (e["dir"] / "transcript.json").exists(),
            "has_notes": (e["dir"] / "notes.json").exists(),
            "has_video": (e["dir"] / "video.mp4").exists(),
            "has_audio": (e["dir"] / "audio.m4a").exists(),
        }
        all_meta.append(meta)

    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_json_dump(all_meta, METADATA_DIR / "all_meetings.json")

    with_id = sum(1 for m in all_meta if m.get("id"))
    videos = sum(1 for m in all_meta if m.get("_local", {}).get("has_video"))
    audios = sum(1 for m in all_meta if m.get("_local", {}).get("has_audio"))

    print(f"\n✅ all_meetings.json updated with {len(all_meta)} meetings")
    print(f"   With embedded metadata: {with_id} | Without embedded metadata: {len(all_meta) - with_id}")
    print(f"   With video: {videos} | With audio: {audios}")
    print(f"   Path: {(METADATA_DIR / 'all_meetings.json').resolve()}")


# ─── API Client ──────────────────────────────────────────────────────────────

class TldvClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        })

    def _get(self, url: str, params: dict = None) -> Optional[dict]:
        """GET JSON with small retry handling for rate limits and transient failures."""
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 10))
                    log.warning(f"  Rate limited. Waiting {wait}s...")
                    time.sleep(wait)
                    continue
                if resp.status_code == 400:
                    log.debug(f"  400 Bad Request — body: {resp.text[:300]}")
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError:
                if resp.status_code in (403, 404):
                    log.warning(f"  {resp.status_code} for {url} - skipping")
                    return None
                if attempt == 2:
                    log.error(f"  Failed after 3 attempts: {resp.status_code}")
                    return None
                time.sleep(2 ** attempt)
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    log.error(f"  Connection error: {e}")
                    return None
                time.sleep(2 ** attempt)
        return None

    def list_all_meetings(self) -> list:
        all_meetings = []
        log.info("📋 Listing all meetings...")

        data = self._get(f"{API_BASE}/meetings")
        if not data:
            log.error("❌ Could not access the API.")
            return []

        if isinstance(data, list):
            log.info(f"✅ Total: {len(data)} meetings found\n")
            return data

        if "results" in data:
            all_meetings.extend(data["results"])
            total = data.get("total", len(all_meetings))
            pages = data.get("pages", 1)
            current_page = data.get("page", 1)
            log.info(f"  Page {current_page}/{pages} — {len(all_meetings)}/{total}")

            # The default API response already represents page 1, so continue after it.
            for page in range(current_page + 1, pages + 1):
                time.sleep(REQUEST_DELAY)
                page_data = self._get(f"{API_BASE}/meetings", params={"page": page})
                if not page_data or "results" not in page_data or not page_data["results"]:
                    break
                all_meetings.extend(page_data["results"])
                log.info(f"  Page {page}/{pages} — {len(all_meetings)}/{total}")

            log.info(f"✅ Total: {len(all_meetings)} meetings found\n")
            return all_meetings

        log.warning(f"  Unexpected response format. Keys: {list(data.keys())}")
        safe_json_dump(data, OUTPUT_DIR / "_debug_meetings_response.json")
        return []

    def get_transcript(self, meeting_id: str) -> Optional[dict]:
        return self._get(f"{API_BASE}/meetings/{meeting_id}/transcript")

    def get_highlights(self, meeting_id: str) -> Optional[dict]:
        return self._get(f"{API_BASE}/meetings/{meeting_id}/highlights")

    def get_download_url(self, meeting_id: str) -> Optional[str]:
        """Return the temporary signed recording URL from the official download endpoint."""
        url = f"{API_BASE}/meetings/{meeting_id}/download"
        for attempt in range(3):
            try:
                resp = self.session.get(url, allow_redirects=False, timeout=30)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 10))
                    log.warning(f"  Rate limited while preparing download. Waiting {wait}s...")
                    time.sleep(wait)
                    continue
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if location:
                        return location
                if resp.status_code in (403, 404):
                    log.warning(f"  {resp.status_code} for recording download {meeting_id} - skipping")
                    return None
                resp.raise_for_status()
                log.warning(f"  Unexpected download response for meeting {meeting_id}: {resp.status_code}")
                return None
            except requests.exceptions.HTTPError:
                if attempt == 2:
                    log.error(f"  Failed to prepare download after 3 attempts: {resp.status_code}")
                    return None
                time.sleep(2 ** attempt)
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    log.error(f"  Connection error while preparing download: {e}")
                    return None
                time.sleep(2 ** attempt)
        return None


# ─── Export Functions ────────────────────────────────────────────────────────

def export_metadata(meetings: list):
    """Persist the API meeting list before any per-meeting export work starts."""
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_json_dump(meetings, METADATA_DIR / "all_meetings.json")
    log.info(f"📁 Metadata saved to {METADATA_DIR / 'all_meetings.json'}")


def export_transcripts_and_notes(client: TldvClient, meetings: list):
    """Export transcript and highlight payloads for each selected meeting."""
    total = len(meetings)
    success_t, success_n = 0, 0
    errors = []

    for i, meeting in enumerate(meetings, 1):
        mid = meeting["id"]
        name = meeting.get("name", "sem_nome")
        date = extract_date_str(meeting.get("happenedAt", ""))
        folder = meeting_folder_name(meeting)
        meeting_dir = MEETINGS_DIR / folder

        log.info(f"[{i}/{total}] {name} ({date})")

        if (meeting_dir / "transcript.json").exists():
            log.info("  Transcript already exists, skipping")
            success_t += 1
        else:
            transcript = client.get_transcript(mid)
            if transcript:
                # Store enough meeting context next to the API payload to support offline rebuilds.
                transcript["_meeting_metadata"] = {
                    "id": mid,
                    "name": name,
                    "happenedAt": meeting.get("happenedAt"),
                    "duration": meeting.get("duration"),
                    "organizer": meeting.get("organizer"),
                    "url": meeting.get("url"),
                }
                safe_json_dump(transcript, meeting_dir / "transcript.json")
                success_t += 1
            else:
                errors.append({"meeting_id": mid, "name": name, "type": "transcript"})

        time.sleep(REQUEST_DELAY)

        if (meeting_dir / "notes.json").exists():
            log.info("  Notes already exist, skipping")
            success_n += 1
        else:
            highlights = client.get_highlights(mid)
            # Meetings without highlights may legitimately return an empty payload.
            if highlights and highlights.get("data"):
                highlights["_meeting_metadata"] = {
                    "id": mid,
                    "name": name,
                    "happenedAt": meeting.get("happenedAt"),
                }
                safe_json_dump(highlights, meeting_dir / "notes.json")
                success_n += 1

        time.sleep(REQUEST_DELAY)

    log.info(f"\n📝 Transcripts exported: {success_t}/{total}")
    log.info(f"📌 Notes exported: {success_n}/{total}")

    if errors:
        safe_json_dump(errors, OUTPUT_DIR / "export_errors.json")
        log.warning(f"⚠️  {len(errors)} errors recorded in export_errors.json")


def download_media(client: TldvClient, meetings: list, audio_only: bool = False, workers: int = 1):
    """Download video or audio assets for the selected meetings."""
    media_label = "audio" if audio_only else "video"
    media_file = "audio.m4a" if audio_only else "video.mp4"
    emoji = "🎵" if audio_only else "🎬"

    if audio_only and not shutil.which("ffmpeg"):
        log.error("❌ ffmpeg not found. Install it with: brew install ffmpeg")
        return

    log.info("🔐 Using the official tl;dv download endpoint with API key authentication.\n")

    # Filter to only meetings that still need downloading
    pending = []
    already = 0
    seen_folders = set()
    for meeting in meetings:
        folder = meeting_folder_name(meeting)
        if folder in seen_folders:
            continue  # skip duplicate folder names
        seen_folders.add(folder)
        output_path = MEETINGS_DIR / folder / media_file
        if output_path.exists():
            already += 1
        else:
            pending.append(meeting)

    if not pending and not already:
        log.info("No meetings to download.")
        return

    if already:
        log.info(f"{emoji} {already} {media_label}s already exist, skipping.")

    if not pending:
        log.info(f"{emoji} All {media_label}s have already been downloaded.")
        return

    # Keep concurrency moderate because the API docs do not publish a hard rate limit number,
    # and each file still needs one signed URL from the tl;dv API before the bulk download starts.
    log.info(f"{emoji} Downloading {len(pending)} {media_label}s" + (f" with {workers} workers...\n" if workers > 1 else "...\n"))

    def _download_signed_file(source_url: str, output_path: Path) -> bool:
        """Stream a signed recording URL to disk with conservative retries."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".part")

        for attempt in range(DOWNLOAD_RETRY_ATTEMPTS):
            if tmp_path.exists():
                tmp_path.unlink()

            try:
                with requests.get(source_url, stream=True, timeout=(30, 300)) as resp:
                    if resp.status_code == 429:
                        wait = int(resp.headers.get("Retry-After", 5 * (attempt + 1)))
                        if attempt == DOWNLOAD_RETRY_ATTEMPTS - 1:
                            log.error(f"    ❌ Download rate-limited after {DOWNLOAD_RETRY_ATTEMPTS} attempts.")
                            return False
                        log.warning(f"    ⏳ Signed URL rate-limited. Waiting {wait}s before retrying...")
                        time.sleep(wait)
                        continue

                    if resp.status_code in (408, 500, 502, 503, 504):
                        if attempt == DOWNLOAD_RETRY_ATTEMPTS - 1:
                            resp.raise_for_status()
                        wait = 2 ** attempt
                        log.warning(f"    ⏳ Temporary download error ({resp.status_code}). Retrying in {wait}s...")
                        time.sleep(wait)
                        continue

                    resp.raise_for_status()
                    with open(tmp_path, "wb") as handle:
                        for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                            if chunk:
                                handle.write(chunk)
                tmp_path.rename(output_path)
                return True
            except requests.exceptions.RequestException as e:
                if attempt == DOWNLOAD_RETRY_ATTEMPTS - 1:
                    if tmp_path.exists():
                        tmp_path.unlink()
                    log.error(f"    ❌ Download failed: {e}")
                    return False
                wait = 2 ** attempt
                log.warning(f"    ⏳ Download request failed ({e}). Retrying in {wait}s...")
                time.sleep(wait)
            except OSError as e:
                if tmp_path.exists():
                    tmp_path.unlink()
                log.error(f"    ❌ Could not write file: {e}")
                return False

        return False

    def _convert_to_audio(input_path: Path, output_path: Path) -> bool:
        """Phase 2: Fast local conversion to AAC m4a."""
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-vn", "-acodec", "aac", "-ab", "64k", "-ac", "1",
            "-y", "-loglevel", "warning",
            str(output_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.error(f"    ❌ Conversion failed: {getattr(e, 'stderr', str(e))[:200]}")
            return False

    def _download_one(idx_meeting):
        """Download a single meeting's media. Returns (success, folder_name)."""
        idx, meeting = idx_meeting
        mid = meeting["id"]
        name = meeting.get("name", "sem_nome")
        date = extract_date_str(meeting.get("happenedAt", ""))
        folder = meeting_folder_name(meeting)
        meeting_dir = MEETINGS_DIR / folder
        output_path = meeting_dir / media_file
        local_video_path = meeting_dir / "video.mp4"

        if output_path.exists():
            return True, folder

        # When video already exists locally, create audio from that file instead of downloading again.
        if audio_only and local_video_path.exists():
            log.info(f"  [{idx}/{len(pending)}] {name} ({date})")
            success = _convert_to_audio(local_video_path, output_path)
            if success:
                log.info(f"    ✅ {folder} (audio extracted from local video)")
            return success, folder

        log.info(f"  [{idx}/{len(pending)}] {name} ({date})")

        # Fetch the signed URL right before the download so we stay well inside its 6-hour TTL.
        source_url = client.get_download_url(mid)
        if not source_url:
            log.warning(f"    ⚠️  Download URL not available: {name}")
            return False, folder

        meeting_dir.mkdir(parents=True, exist_ok=True)
        if audio_only:
            tmp_video_path = meeting_dir / "download.tmp.mp4"
            if tmp_video_path.exists():
                tmp_video_path.unlink()
            success = _download_signed_file(source_url, tmp_video_path)
            if not success:
                return False, folder
            try:
                success = _convert_to_audio(tmp_video_path, output_path)
                if success:
                    log.info(f"    ✅ {folder}")
                return success, folder
            finally:
                if tmp_video_path.exists():
                    tmp_video_path.unlink()

        success = _download_signed_file(source_url, output_path)
        if success:
            log.info(f"    ✅ {folder}")
        return success, folder

    downloaded = 0
    failed = 0

    if workers <= 1:
        for idx, meeting in enumerate(pending, 1):
            success, _ = _download_one((idx, meeting))
            if success:
                downloaded += 1
            else:
                failed += 1
            time.sleep(REQUEST_DELAY)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_download_one, (idx, m)): m
                for idx, m in enumerate(pending, 1)
            }
            for future in as_completed(futures):
                try:
                    success, _ = future.result()
                    if success:
                        downloaded += 1
                    else:
                        failed += 1
                except Exception as e:
                    log.error(f"    ❌ Worker error: {str(e)[:200]}")
                    failed += 1

    log.info(f"\n{emoji} {media_label.capitalize()}s downloaded: {downloaded}/{len(pending)}" +
             (f" ({failed} failures)" if failed else ""))


# ─── Summary ─────────────────────────────────────────────────────────────────

def generate_summary(meetings: list):
    """Summarize what now exists on disk after the export run finishes."""
    transcripts, notes, videos, audios = 0, 0, 0, 0
    if MEETINGS_DIR.exists():
        for d in MEETINGS_DIR.iterdir():
            if d.is_dir():
                transcripts += (d / "transcript.json").exists()
                notes += (d / "notes.json").exists()
                videos += (d / "video.mp4").exists()
                audios += (d / "audio.m4a").exists()

    # Compute date range using parsed dates
    dates = [extract_date_str(m.get("happenedAt", "")) for m in meetings]
    dates = [d for d in dates if d]

    summary = {
        "export_date": datetime.now(timezone.utc).isoformat(),
        "total_meetings": len(meetings),
        "transcriptions_exported": transcripts,
        "notes_exported": notes,
        "videos_downloaded": videos,
        "audios_downloaded": audios,
        "date_range": {
            "earliest": min(dates, default=""),
            "latest": max(dates, default=""),
        },
    }
    safe_json_dump(summary, OUTPUT_DIR / "export_summary.json")

    print("\n" + "=" * 60)
    print("  📊 EXPORT SUMMARY")
    print("=" * 60)
    print(f"  Total meetings:         {summary['total_meetings']}")
    print(f"  Transcripts saved:      {summary['transcriptions_exported']}")
    print(f"  Notes saved:            {summary['notes_exported']}")
    print(f"  Videos downloaded:      {summary['videos_downloaded']}")
    print(f"  Audio files downloaded: {summary['audios_downloaded']}")
    print(f"  Date range:             {summary['date_range']['earliest']} → {summary['date_range']['latest']}")
    print(f"  Output directory:       {OUTPUT_DIR.resolve()}")
    print("=" * 60 + "\n")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    """Coordinate command-only modes, export work, and the final run summary."""
    args = parse_args()
    plan = resolve_export_plan(args)

    print("""
╔══════════════════════════════════════════╗
║      tl;dv Full Export Tool              ║
║      Transcripts · Notes · Media         ║
╚══════════════════════════════════════════╝
    """)

    # These commands operate entirely on local files and return immediately.
    if args["gen_exclude"]:
        generate_exclude_list()
        return
    if args["update_metadata"]:
        update_metadata_from_local()
        return

    # Dry-run stops before any API calls that would list or export meetings.
    if args["dry_run"]:
        print("🔍 DRY-RUN MODE - validating configuration without calling the API\n")

    api_key = get_config()

    if args["dry_run"]:
        print(f"  ✅ API Key:     {api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else f"  ✅ API Key:     {api_key[:4]}...")
        print(f"  {'✅' if shutil.which('ffmpeg') else '❌'} ffmpeg:      {'found' if shutil.which('ffmpeg') else 'NOT found'}")
        print(f"  👷 Workers:     {args['workers']}")
        print(f"  📁 Output:      {OUTPUT_DIR.resolve()}")
        print(f"  🎯 Outputs:     {describe_export_plan(plan)}")
        if args["date_from"] or args["date_to"]:
            f = args['date_from'].strftime('%Y-%m-%d') if args['date_from'] else '(start)'
            t = args['date_to'].strftime('%Y-%m-%d') if args['date_to'] else '(end)'
            print(f"  📅 Date range:  {f} → {t}")
        else:
            print("  📅 Date range:  all meetings")
        retry_ids = load_retry_ids()
        print(f"  🔄 Retry:       {len(retry_ids)} meetings" if retry_ids else f"  🔄 Retry:       no {RETRY_FILE}")
        exclude_ids = load_exclude_ids()
        print(f"  🚫 Exclude:     {len(exclude_ids)} meetings" if exclude_ids else f"  🚫 Exclude:     no {EXCLUDE_FILE}")
        print(f"  📂 Local only:  {'yes (--only-existing)' if args['only_existing'] else 'no'}")
        print("\n✅ Everything looks good. Run without --dry-run to execute.")
        return

    # Log the selected execution plan before the API and file work begins.
    log.info(f"🎯 Selected outputs: {describe_export_plan(plan)}\n")
    if args["date_from"] or args["date_to"]:
        f = args['date_from'].strftime('%Y-%m-%d') if args['date_from'] else '(start)'
        t = args['date_to'].strftime('%Y-%m-%d') if args['date_to'] else '(end)'
        log.info(f"📅 Date filter: {f} → {t}\n")
    if args["only_existing"]:
        log.info("📂 --only-existing mode: local folders only\n")

    client = TldvClient(api_key)

    # 1. Fetch the authoritative meeting list from the API.
    meetings = client.list_all_meetings()
    if not meetings:
        log.error("No meetings found. Check your API key.")
        sys.exit(1)

    meetings.sort(key=lambda m: extract_date_str(m.get("happenedAt", "")))

    # Deduplicate by meeting ID because the API can occasionally repeat entries.
    seen_ids = set()
    unique_meetings = []
    for m in meetings:
        mid = m.get("id")
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            unique_meetings.append(m)
    if len(unique_meetings) < len(meetings):
        log.info(f"🔁 Removed {len(meetings) - len(unique_meetings)} duplicates, {len(unique_meetings)} unique meetings remain\n")
    meetings = unique_meetings

    # 2. Apply optional filters in the order that matches how people typically retry exports.
    retry_ids = load_retry_ids()
    if retry_ids:
        all_count = len(meetings)
        meetings = [m for m in meetings if m["id"] in retry_ids]
        log.info(f"🔄 Retry: {len(meetings)} of {all_count}\n")
        if not meetings:
            log.error("None of the retry meetings were found in the API response.")
            sys.exit(1)

    exclude_ids = load_exclude_ids()
    if exclude_ids:
        before = len(meetings)
        meetings = [m for m in meetings if m["id"] not in exclude_ids]
        log.info(f"🚫 Excluded {before - len(meetings)}, {len(meetings)} remaining\n")
        if not meetings:
            log.info("Everything was excluded. Nothing to do.")
            return

    if args["date_from"] or args["date_to"]:
        before = len(meetings)
        meetings = filter_by_date(meetings, args["date_from"], args["date_to"])
        log.info(f"📅 {len(meetings)} meetings in the selected range (from {before})\n")
        if not meetings:
            log.info("No meetings found in the selected date range.")
            return

    # Only-existing: match against embedded IDs instead of folder names, which may collide.
    if args["only_existing"]:
        before = len(meetings)
        # Build a set of meeting IDs that already exist on disk.
        local_ids = set()
        if MEETINGS_DIR.exists():
            for d in MEETINGS_DIR.iterdir():
                if not d.is_dir():
                    continue
                for fname in ["transcript.json", "notes.json"]:
                    jp = d / fname
                    if jp.exists():
                        try:
                            with open(jp, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            meta = data.get("_meeting_metadata", {})
                            mid = meta.get("id") or data.get("meetingId")
                            if mid:
                                local_ids.add(mid)
                                break
                        except (json.JSONDecodeError, IOError):
                            continue
        meetings = [m for m in meetings if m["id"] in local_ids]
        log.info(f"📂 --only-existing: {len(meetings)} meetings with local folders (from {before})\n")
        if not meetings:
            log.info("No meetings with local folders were found.")
            return

    # 3. Persist the selected set, then export payloads and media.
    export_metadata(meetings)

    if plan["text_exports"]:
        export_transcripts_and_notes(client, meetings)

    # Download video first so a same-run audio pass can reuse the local MP4 instead of re-fetching it.
    if plan["video"]:
        download_media(client, meetings, audio_only=False, workers=args["workers"])
    if plan["audio"]:
        download_media(client, meetings, audio_only=True, workers=args["workers"])

    generate_summary(meetings)
    log.info("🎉 Export completed!")


if __name__ == "__main__":
    main()
