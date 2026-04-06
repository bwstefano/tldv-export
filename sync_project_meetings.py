#!/usr/bin/env python3
"""
sync_project_meetings.py
========================
Copy meetings for one project from the central Drive archive into a specific
project folder, then create or update a project-specific spreadsheet index.

Examples:
    python3 sync_project_meetings.py --project "Project Alpha" --folder-id 1AbCdEfGhIjKlMnOp
    python3 sync_project_meetings.py --project "Project Alpha" --folder-id 1AbCdEfGhIjKlMnOp --dry-run
    python3 sync_project_meetings.py --project "Project Alpha" --folder-id 1AbCdEfGhIjKlMnOp --refresh-existing
"""

import argparse
import csv
import json
import logging
import re
import sys
import unicodedata
from copy import deepcopy
from typing import Dict, List, Optional, Tuple

from upload_to_drive import (
    CLASSIFIED_CSV,
    MIME_FOLDER,
    MIME_SHEET,
    SHEET_COLUMNS,
    UPLOAD_LOG,
    build_drive_client,
    build_sheets_client,
    escape_drive_query_value,
    execute_google_request,
    folder_url_from_id,
    get_or_create_folder,
    get_primary_sheet_info,
    list_existing_files_in_folder,
    load_credentials,
    quote_sheet_title,
    row_get,
    row_set,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


PROJECT_MEETINGS_FOLDER_NAME = "Meetings"
INDEX_SHEET_TEMPLATE = "Meeting Index - {project}"


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for one-project-at-a-time Drive sync."""
    parser = argparse.ArgumentParser(
        description="Copy meetings for one classified project into a specific Drive folder."
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project name to match against project_manual or projects_auto.",
    )
    parser.add_argument(
        "--folder-id",
        required=True,
        help="Drive folder ID of the destination project folder.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be copied without changing Drive.",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Replace already copied files when they exist in the destination folder.",
    )
    return parser.parse_args()


def normalize_project_key(value: str) -> str:
    """Normalize project labels so matching survives accents, case, and dash variations."""
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold()
    normalized = re.sub(r"[‐‑–—−]", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def split_project_values(value: str) -> List[str]:
    """Split multi-project CSV cells while accepting a few common separators."""
    return [part.strip() for part in re.split(r"[|,;]+", value or "") if part.strip()]


def extract_project_candidates(row: Dict[str, str]) -> List[str]:
    """Prefer manual classification when present, otherwise fall back to automatic classification."""
    manual_projects = split_project_values(row_get(row, "project_manual"))
    if manual_projects:
        return manual_projects
    return split_project_values(row_get(row, "projects_auto"))


def row_matches_project(row: Dict[str, str], project_name: str) -> bool:
    """Return True when a row belongs to the selected project."""
    target = normalize_project_key(project_name)
    return any(normalize_project_key(candidate) == target for candidate in extract_project_candidates(row))


def load_classified_rows() -> List[Dict[str, str]]:
    """Load the classified meetings CSV."""
    if not CLASSIFIED_CSV.exists():
        log.error("Classified CSV not found at %s. Run classify_meetings.py first.", CLASSIFIED_CSV)
        sys.exit(1)

    with open(CLASSIFIED_CSV, encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_upload_log() -> Dict[str, str]:
    """Load the central archive mapping from meeting ID to Drive folder URL."""
    if not UPLOAD_LOG.exists():
        log.error("Upload log not found at %s. Run upload_to_drive.py first.", UPLOAD_LOG)
        sys.exit(1)

    with open(UPLOAD_LOG, encoding="utf-8") as handle:
        return json.load(handle)


def folder_id_from_url(url: str) -> Optional[str]:
    """Extract a Drive folder ID from a standard folder URL."""
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", url or "")
    return match.group(1) if match else None


def get_drive_item(drive, file_id: str, fields: str) -> Dict[str, str]:
    """Read Drive metadata for one item."""
    return execute_google_request(
        drive.files().get(
            fileId=file_id,
            fields=fields,
            supportsAllDrives=True,
        ),
        "Read Drive metadata for {}".format(file_id),
    )


def list_drive_children(drive, parent_id: str) -> List[Dict[str, str]]:
    """List every direct child of a Drive folder."""
    items = []
    page_token = None

    while True:
        response = execute_google_request(
            drive.files().list(
                q="'{}' in parents and trashed=false".format(parent_id),
                fields="nextPageToken, files(id, name, mimeType, webViewLink)",
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ),
            "List Drive children in {}".format(parent_id),
        )
        items.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return items


def delete_drive_file(drive, file_id: str) -> None:
    """Delete a Drive file or folder by ID."""
    execute_google_request(
        drive.files().delete(fileId=file_id, supportsAllDrives=True),
        "Delete Drive item {}".format(file_id),
    )


def copy_file_to_folder(drive, source_file: Dict[str, str], parent_id: str) -> Dict[str, str]:
    """Copy one Drive file into a destination folder."""
    return execute_google_request(
        drive.files().copy(
            fileId=source_file["id"],
            body={"name": source_file["name"], "parents": [parent_id]},
            fields="id, webViewLink",
            supportsAllDrives=True,
        ),
        "Copy '{}'".format(source_file["name"]),
    )


def build_project_sheet_rows(rows: List[Dict[str, str]]) -> List[List[str]]:
    """Prepare rows for the project-specific spreadsheet index."""
    values = [[label for _, label in SHEET_COLUMNS]]
    for row in rows:
        values.append([row_get(row, field) for field, _ in SHEET_COLUMNS])
    return values


def create_or_update_index_sheet(sheets, drive, parent_id: str, rows: List[Dict[str, str]], project_name: str) -> str:
    """Create or update the project-specific spreadsheet index."""
    sheet_name = INDEX_SHEET_TEMPLATE.format(project=project_name)
    query = (
        "name='{}' and mimeType='{}' and '{}' in parents and trashed=false".format(
            escape_drive_query_value(sheet_name),
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
        "Lookup spreadsheet '{}'".format(sheet_name),
    ).get("files", [])

    if existing:
        sheet_id = existing[0]["id"]
        log.info("Found existing project spreadsheet %s; updating it.", sheet_id)
    else:
        created = execute_google_request(
            drive.files().create(
                body={
                    "name": sheet_name,
                    "mimeType": MIME_SHEET,
                    "parents": [parent_id],
                },
                fields="id",
                supportsAllDrives=True,
            ),
            "Create spreadsheet '{}'".format(sheet_name),
        )
        sheet_id = created["id"]
        log.info("Created project spreadsheet %s.", sheet_id)

    primary_sheet_id, primary_sheet_title = get_primary_sheet_info(sheets, sheet_id)
    primary_sheet_range = quote_sheet_title(primary_sheet_title)
    sheet_data = build_project_sheet_rows(rows)

    execute_google_request(
        sheets.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range=primary_sheet_range,
        ),
        "Clear sheet '{}'".format(sheet_name),
    )

    execute_google_request(
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range="{}!A1".format(primary_sheet_range),
            valueInputOption="USER_ENTERED",
            body={"values": sheet_data},
        ),
        "Write sheet '{}'".format(sheet_name),
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
        "Format sheet '{}'".format(sheet_name),
    )

    return "https://docs.google.com/spreadsheets/d/{}".format(sheet_id)


def build_project_jobs(project_name: str) -> Tuple[List[Dict[str, str]], int]:
    """
    Build the list of meetings to sync for one project.

    Rows are selected from the classified CSV, while source folders come from
    the central-archive upload log produced by upload_to_drive.py.
    """
    rows = load_classified_rows()
    upload_log = load_upload_log()

    jobs = []
    missing_in_central_archive = 0

    for row in rows:
        if not row_matches_project(row, project_name):
            continue

        meeting_id = row_get(row, "id")
        source_folder_url = upload_log.get(meeting_id)
        if not source_folder_url:
            missing_in_central_archive += 1
            continue

        source_folder_id = folder_id_from_url(source_folder_url)
        if not source_folder_id:
            log.warning("Invalid Drive folder URL in upload log for %s: %s", meeting_id, source_folder_url)
            continue

        jobs.append(
            {
                "meeting_id": meeting_id,
                "source_folder_id": source_folder_id,
                "source_folder_url": source_folder_url,
                "row": row,
            }
        )

    return jobs, missing_in_central_archive


def sync_meeting_folder(drive, source_folder_id: str, destination_parent_id: str, refresh_existing: bool) -> Tuple[str, Dict[str, int]]:
    """Copy every direct file from the source folder into the destination project folder."""
    source_folder = get_drive_item(drive, source_folder_id, "id, name, webViewLink")
    destination_folder_id, _ = get_or_create_folder(drive, source_folder["name"], destination_parent_id)
    destination_files = list_existing_files_in_folder(drive, destination_folder_id)

    copied = 0
    skipped = 0
    replaced = 0

    for item in list_drive_children(drive, source_folder_id):
        if item.get("mimeType") == MIME_FOLDER:
            log.warning("[%s] Nested folder ignored: %s", source_folder["name"], item["name"])
            continue

        existing_id = destination_files.get(item["name"])
        if existing_id:
            if refresh_existing:
                delete_drive_file(drive, existing_id)
                copy_file_to_folder(drive, item, destination_folder_id)
                replaced += 1
                destination_files[item["name"]] = None
            else:
                skipped += 1
            continue

        copy_file_to_folder(drive, item, destination_folder_id)
        copied += 1

    return folder_url_from_id(destination_folder_id), {
        "copied": copied,
        "skipped": skipped,
        "replaced": replaced,
    }


def main() -> None:
    args = parse_args()
    jobs, missing_in_central_archive = build_project_jobs(args.project)

    if not jobs and missing_in_central_archive == 0:
        log.warning("No meetings matched the project '%s'.", args.project)
        return

    log.info(
        "Project '%s': %s meeting(s) ready to sync, %s missing from the central archive.",
        args.project,
        len(jobs),
        missing_in_central_archive,
    )

    if args.dry_run:
        log.info(
            "DRY RUN: meetings would be copied into %s under a '%s' folder.",
            folder_url_from_id(args.folder_id),
            PROJECT_MEETINGS_FOLDER_NAME,
        )
        for index, job in enumerate(jobs[:10], 1):
            row = job["row"]
            log.info(
                "[%s/%s] %s - %s (source: %s)",
                index,
                len(jobs),
                row_get(row, "date"),
                row_get(row, "name"),
                job["source_folder_url"],
            )
        if len(jobs) > 10:
            log.info("... and %s more meeting(s).", len(jobs) - 10)
        return

    log.info("Authenticating with Google...")
    credentials = load_credentials()
    drive = build_drive_client(credentials)
    sheets = build_sheets_client(credentials)
    log.info("Authenticated.")

    meetings_root_id, created_root = get_or_create_folder(
        drive,
        PROJECT_MEETINGS_FOLDER_NAME,
        args.folder_id,
    )
    if created_root:
        log.info("Created '%s' inside the destination project folder.", PROJECT_MEETINGS_FOLDER_NAME)
    log.info("Project meetings folder: %s", folder_url_from_id(meetings_root_id))

    synced_rows = []
    synced_count = 0
    errors = []

    for index, job in enumerate(jobs, 1):
        row = job["row"]
        meeting_label = "{} - {}".format(row_get(row, "date"), row_get(row, "name")).strip(" -")
        log.info("[%s/%s] Syncing %s", index, len(jobs), meeting_label)

        try:
            project_folder_url, stats = sync_meeting_folder(
                drive,
                job["source_folder_id"],
                meetings_root_id,
                args.refresh_existing,
            )
            copied_row = deepcopy(row)
            row_set(copied_row, "drive_url", project_folder_url)
            synced_rows.append(copied_row)
            synced_count += 1
            log.info(
                "    done: %s copied, %s skipped, %s replaced",
                stats["copied"],
                stats["skipped"],
                stats["replaced"],
            )
        except Exception as exc:
            log.error("    failed: %s", exc)
            errors.append(meeting_label)

    synced_rows.sort(key=lambda row: (row_get(row, "date"), row_get(row, "name").lower()))

    log.info("Updating the project spreadsheet index...")
    sheet_url = create_or_update_index_sheet(
        sheets,
        drive,
        args.folder_id,
        synced_rows,
        args.project,
    )

    log.info(
        "Finished. %s meeting(s) synced, %s error(s). Index: %s",
        synced_count,
        len(errors),
        sheet_url,
    )
    if errors:
        log.warning("Meetings with sync errors:")
        for item in errors:
            log.warning("  %s", item)


if __name__ == "__main__":
    main()
