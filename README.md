# tl;dv Export Toolkit

`tldv-export` is a small Python toolkit for exporting tl;dv meetings, classifying them into projects, publishing the archive to Google Drive, and synchronizing project-specific subsets into project folders.

It was created for teams that need more than the tl;dv web interface alone: reproducible exports, reviewable classification rules, spreadsheet indexes, and a structured way to distribute meetings to different project workspaces.

Unless stated otherwise, run the commands below from the repository root.

## What This Repository Includes

- [tldv_export.py](./tldv_export.py): export transcripts, notes, audio, and video from tl;dv through the official API
- [classify_meetings.py](./classify_meetings.py): classify meetings into projects using editable rules plus a manual review loop
- [classification_rules.example.json](./classification_rules.example.json): a starter rules file you can copy and customize
- [upload_to_drive.py](./upload_to_drive.py): upload the exported archive to Google Drive and create a spreadsheet index
- [sync_project_meetings.py](./sync_project_meetings.py): copy only one project's meetings from the central archive into a specific project folder on Drive

## End-to-End Workflow

1. Export meetings from tl;dv with [tldv_export.py](./tldv_export.py)
2. Classify them with [classify_meetings.py](./classify_meetings.py)
3. Review uncertain rows and retrain the ruleset by absorbing manual decisions
4. Upload the full archive to Drive with [upload_to_drive.py](./upload_to_drive.py)
5. Copy one project's relevant meetings into its own Drive folder with [sync_project_meetings.py](./sync_project_meetings.py)

## tl;dv API Access and Plans

This toolkit depends on tl;dv API access.

API access is available for:

- Business
- Enterprise

API access is not available for:

- Free
- Pro

If the API key page is missing or endpoints return `403`, check both your own plan and the meeting organizer's plan.

Official tl;dv references:

- Public API docs: [doc.tldv.io](https://doc.tldv.io/index.html)
- Help Center article: [API and Webhooks](https://intercom.help/tldv/en/articles/11583137-api-and-webhooks)
- API key settings page: [tldv.io/app/settings/personal-settings/api-keys](https://tldv.io/app/settings/personal-settings/api-keys)

## Create a tl;dv API Key

According to tl;dv's public API documentation, you can create an API key from your account settings:

1. Sign in to tl;dv
2. Open [Account Settings > API Keys](https://tldv.io/app/settings/personal-settings/api-keys)
3. Generate a key
4. Save it securely
5. Copy [env_template.env](./env_template.env) to `.env` and paste the key there

Example:

```bash
cp env_template.env .env
```

Then edit `.env`:

```env
TLDV_API_KEY=your_api_key_here
```

## Python and Tooling Requirements

- Python 3.9+
- `requests`
- `ffmpeg` if you want audio extraction
- `google-api-python-client`, `google-auth-httplib2`, and `google-auth-oauthlib` for the Drive scripts

Install the Python dependencies:

```bash
pip install requests google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

Install `ffmpeg` if you need `audio.m4a` outputs:

```bash
brew install ffmpeg
```

## Google Drive Setup

The Drive scripts use OAuth credentials from a Google Cloud project.

1. Open [Google Cloud Console](https://console.cloud.google.com/)
2. Create or choose a project
3. Enable the Google Drive API and Google Sheets API
4. Create an OAuth Client ID of type `Desktop app`
5. Save the downloaded file as `credentials.json` next to the scripts

The first run will open a browser login flow and create `token.json`.

## 1. Export Meetings from tl;dv

[tldv_export.py](./tldv_export.py) uses tl;dv's public API with `x-api-key`.

It:

- lists meetings with `GET /v1alpha1/meetings`
- fetches transcripts with `GET /v1alpha1/meetings/{meetingId}/transcript`
- fetches notes with `GET /v1alpha1/meetings/{meetingId}/highlights`
- downloads recordings with `GET /v1alpha1/meetings/{meetingId}/download`

The download endpoint redirects to a temporary signed URL. The script follows that official flow and stores the media locally.

Examples:

```bash
python3 tldv_export.py
```

```bash
python3 tldv_export.py --with-audio
```

```bash
python3 tldv_export.py --with-audio --with-video
```

```bash
python3 tldv_export.py --from 2026-03-06 --to 2026-04-06
```

```bash
python3 tldv_export.py --dry-run
```

```bash
python3 tldv_export.py --update-metadata
```

The export is written to `tldv_export/`:

```text
tldv_export/
├── export_errors.json
├── export_summary.json
├── meetings/
│   └── 2026-03-30_Project Name/
│       ├── audio.m4a
│       ├── notes.json
│       ├── transcript.json
│       └── video.mp4
└── meetings_metadata/
    └── all_meetings.json
```

## 2. Classify Meetings with Reviewable Rules

[classify_meetings.py](./classify_meetings.py) turns the exported meeting list into a project classification table.

The classifier supports several rule types:

- exact meeting title matches
- keyword matches in meeting titles
- participant email matches
- participant domain matches
- manual overrides by meeting ID
- project aliases for bracket prefixes like `[Project X]`
- conservative project inference across shared conference IDs

To get started, copy the example rules file:

```bash
cp classification_rules.example.json classification_rules.json
```

Run the classifier:

```bash
python3 classify_meetings.py
```

This produces:

- `classified_meetings.csv`: all meetings with automatic and manual classification columns
- `review_queue.csv`: only low-confidence or unclassified meetings
- `classification_stats.txt`: a text summary of coverage by project, method, and confidence

## 3. Manual Review and "Training"

The toolkit's "training" step is intentionally simple and reviewable.

You review `review_queue.csv`, fill the `project_manual` column, and then absorb those decisions back into the rules workflow:

```bash
python3 classify_meetings.py --absorb review_queue_reviewed.csv
```

If repeated manual decisions suggest a stable exact-name rule, the script can propose it and save it back to `classification_rules.json`.

To auto-apply those suggested exact-name rules:

```bash
python3 classify_meetings.py --absorb review_queue_reviewed.csv --apply-suggestions
```

This keeps the logic transparent:

- the rules file is plain JSON
- the review queue is a spreadsheet-friendly CSV
- every retraining step is inspectable and reversible in Git

## 4. Upload the Central Archive to Google Drive

[upload_to_drive.py](./upload_to_drive.py) uploads the local `tldv_export/meetings/` folders to a central Drive archive and creates a spreadsheet index called `Meeting Index`.

It also writes `drive_upload_log.json`, which maps meeting IDs to their Drive folder URLs. That log is used later by the project sync script.

Examples:

```bash
python3 upload_to_drive.py
```

```bash
python3 upload_to_drive.py --only-sheet
```

```bash
python3 upload_to_drive.py --workers 4
```

```bash
python3 upload_to_drive.py --drive-folder-id 1AbCdEfGhIjKlMnOp
```

```bash
python3 upload_to_drive.py --dry-run
```

The spreadsheet index includes meeting title, date, duration, projects, participants, Drive folder URL, and tl;dv URL.

## 5. Copy Project-Specific Meetings into Project Folders

[sync_project_meetings.py](./sync_project_meetings.py) copies meetings for one project at a time from the central archive into a specific Drive folder.

It creates a `Meetings` subfolder inside the destination project folder and a project-specific spreadsheet such as `Meeting Index - Project Alpha`.

Examples:

```bash
python3 sync_project_meetings.py --project "Project Alpha" --folder-id 1AbCdEfGhIjKlMnOp
```

```bash
python3 sync_project_meetings.py --project "Project Alpha" --folder-id 1AbCdEfGhIjKlMnOp --dry-run
```

```bash
python3 sync_project_meetings.py --project "Project Alpha" --folder-id 1AbCdEfGhIjKlMnOp --refresh-existing
```

This script does not re-upload local files from your computer. It copies existing files inside Google Drive from the central archive into the destination project folder.

## Notes and Limitations

- The toolkit only exports meetings the API key is allowed to access.
- UI visibility in tl;dv does not automatically guarantee API export access.
- Recording downloads depend on tl;dv's temporary signed media URLs.
- `ffmpeg` is only required when generating audio-only outputs.
- The Drive scripts require Google APIs and OAuth credentials.
- Project synchronization depends on `drive_upload_log.json`, so you should keep that file alongside the archive workflow.

## Repository Hygiene

This repository intentionally excludes:

- real API keys
- real Google credentials
- local export outputs
- upload logs
- organization-specific project names and folder IDs

Customize the rules, Drive folder IDs, and project names for your own environment.

🤖 Created with Codex.
