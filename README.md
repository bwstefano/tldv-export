# tl;dv Full Export

`tldv_export.py` is a single-file Python utility for exporting the tl;dv data you can access through the public API.

It was created for practical archival and operations work: pulling transcripts and notes in structured JSON, downloading meeting recordings when needed, and producing a local export that can later be uploaded, indexed, searched, or transformed for other workflows.

## What It Does

- Exports meeting transcripts as `transcript.json`
- Exports meeting notes/highlights as `notes.json`
- Downloads meeting recordings as `video.mp4`
- Extracts audio-only files as `audio.m4a`
- Saves a full local meeting index in `all_meetings.json`
- Supports date filters, retry lists, exclusion lists, and metadata rebuilds

## Why This Exists

The tl;dv web app is useful for browsing and sharing meetings, but many real-world workflows need a full local export:

- long-term archiving
- migrations to other systems
- Drive publishing
- AI indexing and summarization pipelines
- custom reporting or analytics

This script packages those tasks into one file with minimal dependencies.

## How It Works

The script uses tl;dv's public API with an `x-api-key` header.

At a high level it:

1. Lists meetings with `GET /v1alpha1/meetings`
2. Fetches transcripts with `GET /v1alpha1/meetings/{meetingId}/transcript`
3. Fetches notes/highlights with `GET /v1alpha1/meetings/{meetingId}/highlights`
4. Downloads recordings through `GET /v1alpha1/meetings/{meetingId}/download`

For recordings, tl;dv returns an HTTP redirect to a signed URL with a limited lifetime. This script follows that official flow and stores the downloaded file locally.

## Requirements

- Python 3.8+
- `requests`
- `ffmpeg` only if you want `--with-audio`

Install the Python dependency:

```bash
pip install requests
```

Install `ffmpeg` if you want audio extraction:

```bash
brew install ffmpeg
```

## tl;dv API Access and Plans

This script depends on tl;dv API access.

API access is available for:

- Business
- Enterprise

API access is not available for:

- Free
- Pro

Practical recommendation:

- If the API key page is missing or endpoints return `403`, check both your own plan and the meeting organizer's plan.
- If you plan to use this script in production, confirm current availability with tl;dv before depending on it operationally.

Official references:

- Public API docs: [doc.tldv.io](https://doc.tldv.io/index.html)
- Help Center article: [API and Webhooks](https://intercom.help/tldv/en/articles/11583137-api-and-webhooks)

## Create an API Key

According to tl;dv's public API documentation, you can create an API key from your personal settings:

1. Sign in to tl;dv
2. Open [Account Settings > API Keys](https://tldv.io/app/settings/personal-settings/api-keys)
3. Generate a new key
4. Store it securely and do not commit it to Git

Useful official links:

- API keys page: [tldv.io/app/settings/personal-settings/api-keys](https://tldv.io/app/settings/personal-settings/api-keys)
- API overview and usage: [doc.tldv.io](https://doc.tldv.io/index.html)
- Help Center overview: [API and Webhooks](https://intercom.help/tldv/en/articles/11583137-api-and-webhooks)

## Setup

Copy the template and add your key:

```bash
cp env_template.env .env
```

Then edit `.env`:

```env
TLDV_API_KEY=your_api_key_here
```

## Usage

Export transcripts and notes only:

```bash
python3 tldv_export.py
```

Validate config and selection without exporting:

```bash
python3 tldv_export.py --dry-run
```

Export transcripts, notes, and audio:

```bash
python3 tldv_export.py --with-audio
```

Export transcripts, notes, audio, and video:

```bash
python3 tldv_export.py --with-audio --with-video
```

Download video without audio extraction:

```bash
python3 tldv_export.py --with-video
```

Restrict the export to meetings on or after a given date:

```bash
python3 tldv_export.py --from 2026-03-06
```

Restrict the export to a date range:

```bash
python3 tldv_export.py --from 2026-03-06 --to 2026-04-06
```

Add audio for meetings that already exist locally:

```bash
python3 tldv_export.py --with-audio --only-existing
```

Run with a custom worker count for parallel media downloads:

```bash
python3 tldv_export.py --with-audio --with-video --workers 4
```

Generate an exclusion list from already exported meetings:

```bash
python3 tldv_export.py --generate-exclude
```

Rebuild the metadata index from the local export only:

```bash
python3 tldv_export.py --update-metadata
```

## Retry Failed Meetings

To retry only specific meetings, create a `retry_meetings.txt` file in the same folder as the script.

You can put either:

- raw meeting IDs, one per line
- log lines that contain meeting IDs

When the file exists, the script will restrict processing to those meetings.

## Output Structure

The export is written to a local `tldv_export/` directory:

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

## Notes and Limitations

- The script only exports meetings that your API key is allowed to access.
- UI visibility in tl;dv does not automatically guarantee API export access.
- Recording downloads use tl;dv's signed download URL flow, which is temporary by design.
- `ffmpeg` is only needed when generating `audio.m4a`.
- The script is intentionally dependency-light and does not use `argparse`.

## Official tl;dv References

- Public API docs: [https://doc.tldv.io/index.html](https://doc.tldv.io/index.html)
- Help Center: [https://intercom.help/tldv/en/articles/11583137-api-and-webhooks](https://intercom.help/tldv/en/articles/11583137-api-and-webhooks)
- API key settings: [https://tldv.io/app/settings/personal-settings/api-keys](https://tldv.io/app/settings/personal-settings/api-keys)

🤖 Created with Codex.
