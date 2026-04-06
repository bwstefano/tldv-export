#!/usr/bin/env python3
"""
upload_to_drive.py
==================
Upload exported tl;dv meeting folders to Google Drive and create or update a
spreadsheet index at the archive root.

Requirements:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

Google setup:
    1. Create a Google Cloud project
    2. Enable the Google Drive API and Google Sheets API
    3. Create an OAuth Client ID of type "Desktop app"
    4. Save the downloaded credentials as credentials.json next to this script

Examples:
    python3 upload_to_drive.py
    python3 upload_to_drive.py --only-sheet
    python3 upload_to_drive.py --workers 4
    python3 upload_to_drive.py --drive-folder-id 1AbCdEfGhIjKlMnOp
    python3 upload_to_drive.py --dry-run
"""

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


LOCAL_MEETINGS_DIR = Path("tldv_export/meetings")
CLASSIFIED_CSV = Path("classified_meetings.csv")
UPLOAD_LOG = Path("drive_upload_log.json")

ROOT_FOLDER_NAME = "tl;dv Meetings"
INDEX_SHEET_NAME = "Meeting Index"

CREDENTIALS_FILE = Path("credentials.json")
TOKEN_FILE = Path("token.json")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"

IGNORED_UPLOAD_FILENAMES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}

DEFAULT_WORKERS = 4
MAX_WORKERS = 8
MAX_API_RETRIES = 5
MAX_BACKOFF_SECONDS = 30

ROW_FIELD_ALIASES = {
    "id": ["id"],
    "name": ["name", "nome"],
    "date": ["date", "data"],
    "duration_min": ["duration_min", "duracao_min"],
    "participants": ["participants", "participantes"],
    "participant_count": ["participant_count", "num_participantes"],
    "projects_auto": ["projects_auto", "projetos_auto"],
    "project_manual": ["project_manual", "projeto_manual"],
    "confidence": ["confidence", "confianca"],
    "method": ["method", "metodo"],
    "conference_id": ["conference_id", "conferenceId"],
    "meeting_url": ["meeting_url", "url"],
    "drive_url": ["drive_url"],
}

SHEET_COLUMNS = [
    ("name", "Meeting Name"),
    ("date", "Date"),
    ("duration_min", "Duration (min)"),
    ("projects_auto", "Projects (auto)"),
    ("project_manual", "Project (manual)"),
    ("confidence", "Confidence"),
    ("participants", "Participants"),
    ("drive_url", "Drive Folder"),
    ("meeting_url", "tl;dv URL"),
    ("id", "Meeting ID"),
]


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for uploading and indexing."""
    parser = argparse.ArgumentParser(
        description="Upload exported tl;dv meeting folders to Google Drive."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing to Google Drive.",
    )
    parser.add_argument(
        "--only-sheet",
        action="store_true",
        help="Skip file uploads and only create or update the spreadsheet index.",
    )
    parser.add_argument(
        "--drive-folder-id",
        default=None,
        help="Existing Drive folder ID to use as the archive root.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Parallel meeting uploads to run at once (1-8).",
    )
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be at least 1.")
    if args.workers > MAX_WORKERS:
        log.warning(
            "--workers=%s is above the recommended limit for this script; using %s instead.",
            args.workers,
            MAX_WORKERS,
        )
        args.workers = MAX_WORKERS
    return args


def row_get(row: Dict[str, str], canonical_field: str, default: str = "") -> str:
    """Read a row value by canonical field name while staying compatible with legacy headers."""
    for key in ROW_FIELD_ALIASES.get(canonical_field, [canonical_field]):
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def row_set(row: Dict[str, str], canonical_field: str, value: str) -> None:
    """Write to the first matching key in a row, or create the canonical key if it does not exist."""
    for key in ROW_FIELD_ALIASES.get(canonical_field, [canonical_field]):
        if key in row:
            row[key] = value
            return
    row[canonical_field] = value


def sanitize_filename(name: str) -> str:
    """Mirror the filename sanitization used by tldv_export.py."""
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    return name.strip().strip(".")[:200]


def extract_date_str(happened_at: str) -> str:
    """Extract a YYYY-MM-DD date from either ISO timestamps or already formatted dates."""
    if not happened_at:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", happened_at):
        return happened_at[:10]
    match = re.match(r"\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})", happened_at)
    if not match:
        return ""

    months = {
        "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
        "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
        "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
    }
    month = months.get(match.group(1), "01")
    day = match.group(2).zfill(2)
    year = match.group(3)
    return "{}-{}-{}".format(year, month, day)


def meeting_folder_name(meeting_name: str, happened_at: str) -> str:
    """Rebuild the local folder name so the uploader can match CSV rows to exported folders."""
    date = extract_date_str(happened_at)
    return sanitize_filename("{}_{}".format(date, meeting_name))


def should_ignore_upload_file(local_path: Path) -> bool:
    """Skip operating-system metadata files that should never become Drive artifacts."""
    return local_path.name in IGNORED_UPLOAD_FILENAMES or local_path.name.startswith("._")


def escape_drive_query_value(value: str) -> str:
    """Escape a text fragment before embedding it inside a Drive API query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def is_retryable_http_error(error: Exception) -> bool:
    """Detect Google API responses that merit an automatic retry."""
    status = getattr(getattr(error, "resp", None), "status", None)
    if status in {429, 500, 502, 503, 504}:
        return True
    if status != 403:
        return False

    content = getattr(error, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")

    retry_markers = ("rateLimitExceeded", "userRateLimitExceeded", "backendError")
    return any(marker in content for marker in retry_markers)


def execute_google_request(request, description: str):
    """Execute a Google API request with conservative retries for transient failures."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        HttpError = Exception

    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as exc:
            if attempt >= MAX_API_RETRIES or not is_retryable_http_error(exc):
                raise

            sleep_seconds = min((2 ** (attempt - 1)) + random.random(), MAX_BACKOFF_SECONDS)
            status = getattr(getattr(exc, "resp", None), "status", "unknown")
            log.warning(
                "%s failed with HTTP %s; retrying in %.1fs (%s/%s).",
                description,
                status,
                sleep_seconds,
                attempt,
                MAX_API_RETRIES,
            )
            time.sleep(sleep_seconds)


def load_credentials():
    """Authenticate with Google OAuth2 and return reusable credentials."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        log.error(
            "Missing dependencies. Run:\n"
            "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )
        sys.exit(1)

    credentials = None

    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                log.error(
                    "Credentials file not found at %s.\n"
                    "Download a Google OAuth desktop client JSON and save it as credentials.json.",
                    CREDENTIALS_FILE,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            credentials = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")
        log.info("Saved OAuth token to %s.", TOKEN_FILE)

    return credentials


def build_drive_client(credentials):
    """Build a Drive client."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def build_sheets_client(credentials):
    """Build a Sheets client."""
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def build_thread_drive_client(serialized_credentials: str):
    """Each worker uses its own Drive client because google-api-python-client is not thread-safe."""
    from google.oauth2.credentials import Credentials

    credentials_info = json.loads(serialized_credentials)
    credentials = Credentials.from_authorized_user_info(credentials_info, SCOPES)
    return build_drive_client(credentials)


def get_or_create_folder(drive, name: str, parent_id: Optional[str] = None) -> Tuple[str, bool]:
    """Return an existing folder ID or create the folder when missing."""
    escaped_name = escape_drive_query_value(name)
    query = "name='{}' and mimeType='{}' and trashed=false".format(escaped_name, MIME_FOLDER)
    if parent_id:
        query += " and '{}' in parents".format(parent_id)

    results = execute_google_request(
        drive.files().list(
            q=query,
            fields="files(id, name)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ),
        "Lookup folder '{}'".format(name),
    )
    files = results.get("files", [])
    if files:
        return files[0]["id"], False

    metadata = {"name": name, "mimeType": MIME_FOLDER}
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = execute_google_request(
        drive.files().create(body=metadata, fields="id", supportsAllDrives=True),
        "Create folder '{}'".format(name),
    )
    return folder["id"], True


def list_existing_files_in_folder(drive, parent_id: str) -> Dict[str, str]:
    """List files already present in a folder so uploads can skip duplicates efficiently."""
    existing = {}
    page_token = None

    while True:
        response = execute_google_request(
            drive.files().list(
                q="'{}' in parents and trashed=false".format(parent_id),
                fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ),
            "List files in folder {}".format(parent_id),
        )

        for item in response.get("files", []):
            if item.get("mimeType") != MIME_FOLDER:
                existing[item["name"]] = item["id"]

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return existing


def upload_file(drive, local_path: Path, parent_id: str) -> Optional[str]:
    """Upload one file to Drive and return its file ID, or None when ignored."""
    from googleapiclient.http import MediaFileUpload

    if should_ignore_upload_file(local_path):
        return None

    mime_map = {
        ".json": "application/json",
        ".txt": "text/plain",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "video/mp4",
        ".pdf": "application/pdf",
    }
    mime_type = mime_map.get(local_path.suffix.lower(), "application/octet-stream")
    metadata = {"name": local_path.name, "parents": [parent_id]}
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

    uploaded = execute_google_request(
        drive.files().create(
            body=metadata,
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ),
        "Upload file '{}'".format(local_path.name),
    )
    return uploaded["id"]


def upload_meeting_folder(drive, local_folder: Path, parent_id: str) -> str:
    """Create or reuse a Drive folder, then upload all useful files from the local meeting folder."""
    folder_id, created_now = get_or_create_folder(drive, local_folder.name, parent_id)
    existing_files = {} if created_now else list_existing_files_in_folder(drive, folder_id)
    prefix = "[{}]".format(local_folder.name)

    for local_path in sorted(local_folder.iterdir()):
        if not local_path.is_file():
            continue

        if should_ignore_upload_file(local_path):
            log.info("%s skipped %s", prefix, local_path.name)
            continue

        if local_path.name in existing_files:
            log.info("%s already exists %s", prefix, local_path.name)
            continue

        file_id = upload_file(drive, local_path, folder_id)
        if file_id:
            log.info("%s uploaded %s", prefix, local_path.name)

    return folder_url_from_id(folder_id)


def folder_url_from_id(folder_id: str) -> str:
    """Build a browser URL for a Drive folder."""
    return "https://drive.google.com/drive/folders/{}".format(folder_id)


def quote_sheet_title(title: str) -> str:
    """Escape a sheet tab title for A1 notation."""
    return "'" + title.replace("'", "''") + "'"


def get_primary_sheet_info(sheets, spreadsheet_id: str) -> Tuple[int, str]:
    """Return the first tab's sheet ID and title regardless of the user's locale."""
    metadata = execute_google_request(
        sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title,index))",
        ),
        "Read spreadsheet tabs {}".format(spreadsheet_id),
    )
    tabs = metadata.get("sheets", [])
    if not tabs:
        raise RuntimeError("Spreadsheet {} does not contain any tabs.".format(spreadsheet_id))

    primary = min(
        (sheet.get("properties", {}) for sheet in tabs),
        key=lambda props: props.get("index", 0),
    )
    return primary["sheetId"], primary["title"]


def build_sheet_rows(rows: Sequence[Dict[str, str]]) -> List[List[str]]:
    """Translate CSV rows into the two-dimensional structure expected by the Sheets API."""
    values = [[label for _, label in SHEET_COLUMNS]]
    for row in rows:
        values.append([row_get(row, field) for field, _ in SHEET_COLUMNS])
    return values


def create_or_update_index_sheet(sheets, drive, parent_id: str, rows: Sequence[Dict[str, str]]) -> str:
    """Create or update the archive spreadsheet index and return its URL."""
    query = (
        "name='{}' and mimeType='{}' and '{}' in parents and trashed=false".format(
            escape_drive_query_value(INDEX_SHEET_NAME),
            MIME_SHEET,
            parent_id,
        )
    )
    existing = execute_google_request(
        drive.files().list(
            q=query,
            fields="files(id)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ),
        "Lookup spreadsheet '{}'".format(INDEX_SHEET_NAME),
    ).get("files", [])

    if existing:
        sheet_id = existing[0]["id"]
        log.info("Found existing spreadsheet %s; updating it.", sheet_id)
    else:
        metadata = {
            "name": INDEX_SHEET_NAME,
            "mimeType": MIME_SHEET,
            "parents": [parent_id],
        }
        created = execute_google_request(
            drive.files().create(body=metadata, fields="id", supportsAllDrives=True),
            "Create spreadsheet '{}'".format(INDEX_SHEET_NAME),
        )
        sheet_id = created["id"]
        log.info("Created spreadsheet %s.", sheet_id)

    spreadsheet_url = "https://docs.google.com/spreadsheets/d/{}".format(sheet_id)
    primary_sheet_id, primary_sheet_title = get_primary_sheet_info(sheets, sheet_id)
    primary_sheet_range = quote_sheet_title(primary_sheet_title)
    sheet_data = build_sheet_rows(rows)

    execute_google_request(
        sheets.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range=primary_sheet_range,
        ),
        "Clear sheet '{}'".format(INDEX_SHEET_NAME),
    )

    execute_google_request(
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range="{}!A1".format(primary_sheet_range),
            valueInputOption="USER_ENTERED",
            body={"values": sheet_data},
        ),
        "Write sheet '{}'".format(INDEX_SHEET_NAME),
    )

    column_count = len(SHEET_COLUMNS)
    execute_google_request(
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": primary_sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85},
                                    "textFormat": {"bold": True},
                                }
                            },
                            "fields": "userEnteredFormat(backgroundColor,textFormat)",
                        }
                    },
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": primary_sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": column_count,
                            }
                        }
                    },
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": primary_sheet_id,
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "gridProperties.frozenRowCount",
                        }
                    },
                ]
            },
        ),
        "Format sheet '{}'".format(INDEX_SHEET_NAME),
    )

    log.info("Spreadsheet ready: %s", spreadsheet_url)
    return spreadsheet_url


def load_classified_csv(path: Path) -> List[Dict[str, str]]:
    """Load the classified meetings CSV."""
    with open(path, encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def save_classified_csv(rows: List[Dict[str, str]], path: Path) -> None:
    """Save the classified CSV while preserving any columns that already exist."""
    if not rows:
        return

    fieldnames = list(rows[0].keys())
    if "drive_url" not in fieldnames:
        fieldnames.append("drive_url")

    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_upload_log() -> Dict[str, str]:
    """Read the JSON log that maps meeting IDs to Drive folder URLs."""
    if not UPLOAD_LOG.exists():
        return {}
    with open(UPLOAD_LOG, encoding="utf-8") as handle:
        return json.load(handle)


def save_upload_log(log_data: Dict[str, str]) -> None:
    """Persist the upload log after successful uploads."""
    with open(UPLOAD_LOG, "w", encoding="utf-8") as handle:
        json.dump(log_data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def upload_meeting_worker(serialized_credentials: str, local_folder: str, parent_id: str) -> str:
    """Thread worker that uploads one meeting folder using its own Drive client."""
    drive = build_thread_drive_client(serialized_credentials)
    return upload_meeting_folder(drive, Path(local_folder), parent_id)


def main() -> None:
    args = parse_args()

    if not CLASSIFIED_CSV.exists():
        log.error("Classified CSV not found at %s. Run classify_meetings.py first.", CLASSIFIED_CSV)
        sys.exit(1)

    rows = load_classified_csv(CLASSIFIED_CSV)
    log.info("Loaded %s meetings from %s.", len(rows), CLASSIFIED_CSV)

    upload_log = load_upload_log()
    log.info("Loaded %s prior uploads from %s.", len(upload_log), UPLOAD_LOG)

    if args.dry_run:
        log.info("=== DRY RUN: no changes will be made ===")
        log.info("Workers configured: %s", args.workers)
        missing = []
        for row in rows:
            folder_name = meeting_folder_name(row_get(row, "name"), row_get(row, "date"))
            local_path = LOCAL_MEETINGS_DIR / folder_name
            if not local_path.exists():
                missing.append(folder_name)
        log.info("Local meeting folders found: %s/%s", len(rows) - len(missing), len(rows))
        if missing:
            log.warning("Local folders not found (%s):", len(missing))
            for item in missing[:10]:
                log.warning("  %s", item)
            if len(missing) > 10:
                log.warning("  ... and %s more", len(missing) - 10)
        return

    log.info("Authenticating with Google...")
    credentials = load_credentials()
    drive = build_drive_client(credentials)
    sheets = build_sheets_client(credentials)
    serialized_credentials = credentials.to_json()
    log.info("Authenticated.")

    if args.drive_folder_id:
        root_id = args.drive_folder_id
        log.info("Using existing Drive root folder: %s", folder_url_from_id(root_id))
    else:
        root_id, _ = get_or_create_folder(drive, ROOT_FOLDER_NAME)
        log.info("Drive root folder: %s", folder_url_from_id(root_id))

    if not args.only_sheet:
        total = len(rows)
        completed_uploads = 0
        skipped_uploads = 0
        errors = []
        jobs = []

        for position, row in enumerate(rows, 1):
            meeting_id = row_get(row, "id")
            folder_name = meeting_folder_name(row_get(row, "name"), row_get(row, "date"))
            local_path = LOCAL_MEETINGS_DIR / folder_name

            if meeting_id in upload_log:
                row_set(row, "drive_url", upload_log[meeting_id])
                skipped_uploads += 1
                continue

            if not local_path.exists():
                log.warning("[%s/%s] Local folder not found: %s", position, total, folder_name)
                errors.append(folder_name)
                continue

            jobs.append(
                {
                    "position": position,
                    "row": row,
                    "meeting_id": meeting_id,
                    "folder_name": folder_name,
                    "local_path": local_path,
                }
            )

        if jobs:
            log.info("Uploading %s meetings with %s worker(s).", len(jobs), args.workers)

        if args.workers == 1:
            for job in jobs:
                log.info("[%s/%s] Uploading %s", job["position"], total, job["folder_name"])
                try:
                    drive_url = upload_meeting_folder(drive, job["local_path"], root_id)
                    row_set(job["row"], "drive_url", drive_url)
                    upload_log[job["meeting_id"]] = drive_url
                    completed_uploads += 1
                    if completed_uploads % 10 == 0:
                        save_upload_log(upload_log)
                except Exception as exc:
                    log.error("Upload failed for %s: %s", job["folder_name"], exc)
                    errors.append(job["folder_name"])
        else:
            future_to_job = {}
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                for job in jobs:
                    future = executor.submit(
                        upload_meeting_worker,
                        serialized_credentials,
                        str(job["local_path"]),
                        root_id,
                    )
                    future_to_job[future] = job

                for completed_position, future in enumerate(as_completed(future_to_job), 1):
                    job = future_to_job[future]
                    try:
                        drive_url = future.result()
                        row_set(job["row"], "drive_url", drive_url)
                        upload_log[job["meeting_id"]] = drive_url
                        completed_uploads += 1
                        log.info(
                            "[%s/%s] Uploaded %s",
                            completed_position,
                            len(jobs),
                            job["folder_name"],
                        )
                        if completed_uploads % 10 == 0:
                            save_upload_log(upload_log)
                    except Exception as exc:
                        log.error(
                            "[%s/%s] Upload failed for %s: %s",
                            job["position"],
                            total,
                            job["folder_name"],
                            exc,
                        )
                        errors.append(job["folder_name"])

        save_upload_log(upload_log)
        save_classified_csv(rows, CLASSIFIED_CSV)

        log.info(
            "Upload finished: %s new, %s already logged, %s errors.",
            completed_uploads,
            skipped_uploads,
            len(errors),
        )
        if errors:
            log.warning("Meetings with upload errors:")
            for item in errors:
                log.warning("  %s", item)
    else:
        for row in rows:
            meeting_id = row_get(row, "id")
            if meeting_id in upload_log:
                row_set(row, "drive_url", upload_log[meeting_id])

    log.info("Creating or updating the spreadsheet index...")
    sheet_url = create_or_update_index_sheet(sheets, drive, root_id, rows)
    log.info("Done. Spreadsheet index: %s", sheet_url)


if __name__ == "__main__":
    main()
