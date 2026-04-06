#!/usr/bin/env python3
"""
classify_meetings.py
====================
Classify tl;dv meetings into projects using a configurable ruleset and a
review-and-retrain workflow.

Typical workflow:
    1. Export meetings with tldv_export.py
    2. Copy classification_rules.example.json to classification_rules.json
    3. Run this script to generate classified_meetings.csv and review_queue.csv
    4. Review low-confidence rows and fill project_manual
    5. Re-run with --absorb to fold reviewed decisions back into the rules

Examples:
    python3 classify_meetings.py
    python3 classify_meetings.py --rules classification_rules.json
    python3 classify_meetings.py --absorb review_queue_reviewed.csv
    python3 classify_meetings.py --apply-suggestions --absorb review_queue_reviewed.csv
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


DEFAULT_MEETINGS_FILE = Path("tldv_export/meetings_metadata/all_meetings.json")
DEFAULT_RULES_FILE = Path("classification_rules.json")
RULES_TEMPLATE_FILE = Path("classification_rules.example.json")

FULL_OUTPUT_COLUMNS = [
    "id",
    "name",
    "date",
    "duration_min",
    "participants",
    "participant_count",
    "projects_auto",
    "confidence",
    "method",
    "project_manual",
    "conference_id",
    "meeting_url",
]

REVIEW_QUEUE_COLUMNS = [
    "id",
    "name",
    "date",
    "duration_min",
    "participants",
    "projects_auto",
    "confidence",
    "method",
    "project_manual",
    "meeting_url",
]


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the classifier workflow."""
    parser = argparse.ArgumentParser(
        description="Classify tl;dv meetings into projects with rules plus manual review."
    )
    parser.add_argument(
        "--meetings",
        default=str(DEFAULT_MEETINGS_FILE),
        help="Path to the meetings metadata JSON exported by tldv_export.py.",
    )
    parser.add_argument(
        "--rules",
        default=str(DEFAULT_RULES_FILE),
        help="Path to the editable classification rules JSON file.",
    )
    parser.add_argument(
        "--out",
        default=".",
        help="Directory where CSV and stats files will be written.",
    )
    parser.add_argument(
        "--absorb",
        default=None,
        help="Reviewed CSV file whose project_manual values should be absorbed back into the workflow.",
    )
    parser.add_argument(
        "--apply-suggestions",
        action="store_true",
        help="Apply suggested exact-name rules automatically when absorbing manual reviews.",
    )
    return parser.parse_args()


def load_json(path: Path) -> object:
    """Read a JSON file from disk."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def ensure_project_list(value: object) -> List[str]:
    """Normalize project values so every rule produces a list of project names."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def normalize_rules(raw_rules: Dict[str, object]) -> Dict[str, object]:
    """
    Convert the on-disk rules file into a stable internal structure.

    The repository uses English keys, but the loader also accepts the earlier
    Portuguese field names to make migrations easier.
    """
    normalized = {
        "project_aliases": raw_rules.get("project_aliases", {}),
        "manual_overrides": raw_rules.get("manual_overrides", raw_rules.get("substituicoes_manuais", {})),
        "exact_name_rules": raw_rules.get("exact_name_rules", raw_rules.get("regras_nome_exato", {})),
        "keyword_rules": [],
        "participant_email_rules": [],
        "participant_domain_rules": [],
    }

    for name, value in list(normalized["exact_name_rules"].items()):
        normalized["exact_name_rules"][name] = ensure_project_list(value)

    for meeting_id, value in list(normalized["manual_overrides"].items()):
        normalized["manual_overrides"][meeting_id] = ensure_project_list(value)

    for rule in raw_rules.get("keyword_rules", raw_rules.get("regras_palavra_chave", [])):
        pattern = rule.get("pattern", rule.get("padrao", ""))
        projects = ensure_project_list(rule.get("projects", rule.get("projetos", rule.get("projeto"))))
        if not pattern or not projects:
            continue
        normalized["keyword_rules"].append(
            {
                "pattern": pattern,
                "projects": projects,
                "confidence": rule.get("confidence", rule.get("confianca", "high")),
                "comment": rule.get("comment", rule.get("_comment", "")),
            }
        )

    for rule in raw_rules.get("participant_email_rules", raw_rules.get("regras_email_participante", [])):
        emails = [str(email).strip().lower() for email in rule.get("emails", []) if str(email).strip()]
        projects = ensure_project_list(rule.get("projects", rule.get("projetos", rule.get("projeto"))))
        if not emails or not projects:
            continue
        normalized["participant_email_rules"].append(
            {
                "emails": emails,
                "projects": projects,
                "confidence": rule.get("confidence", rule.get("confianca", "medium")),
            }
        )

    for rule in raw_rules.get("participant_domain_rules", raw_rules.get("regras_dominio_participante", [])):
        domain = str(rule.get("domain", rule.get("dominio", ""))).strip().lower()
        projects = ensure_project_list(rule.get("projects", rule.get("projetos", rule.get("projeto"))))
        if not domain or not projects:
            continue
        normalized["participant_domain_rules"].append(
            {
                "domain": domain,
                "projects": projects,
                "confidence": rule.get("confidence", rule.get("confianca", "low")),
            }
        )

    return normalized


def save_rules(path: Path, rules: Dict[str, object]) -> None:
    """Persist the normalized rules back to disk using English keys only."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rules, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def load_rules(path: Path) -> Dict[str, object]:
    """Load and normalize a rules file, failing with a helpful message if it is missing."""
    if not path.exists():
        print(f"Error: rules file not found at {path}")
        if RULES_TEMPLATE_FILE.exists():
            print(
                f"Create it by copying {RULES_TEMPLATE_FILE}:\n"
                f"  cp {RULES_TEMPLATE_FILE} {path}"
            )
        sys.exit(1)
    return normalize_rules(load_json(path))


def extract_bracket_prefix(name: str) -> Optional[str]:
    """Return the project-like prefix from a meeting title such as [Project X] Daily Sync."""
    match = re.match(r"^\[([^\]]+)\]", (name or "").strip())
    return match.group(1).strip() if match else None


def normalize_project_name(name: str, aliases: Dict[str, str]) -> str:
    """Apply optional user-defined aliases so bracket prefixes land on canonical project names."""
    return aliases.get(name, name)


def parse_happened_at(value: str) -> Optional[datetime]:
    """Best-effort parsing for the meeting date strings returned by tl;dv metadata."""
    if not value:
        return None

    if re.match(r"^\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    candidates = [value, value[:24], value[:29], value[:33]]
    for candidate in candidates:
        for fmt in (
            "%a %b %d %Y %H:%M:%S",
            "%a %b %d %Y %H:%M:%S GMT%z",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def format_date(value: str) -> str:
    """Render a meeting date in YYYY-MM-DD whenever possible."""
    parsed = parse_happened_at(value)
    if parsed:
        return parsed.strftime("%Y-%m-%d")
    return value[:10] if value else ""


def format_participants(invitees: Iterable[Dict[str, object]], organizer: Optional[Dict[str, object]] = None) -> str:
    """Create a compact participants string that still fits comfortably in CSV and Sheets."""
    emails = [str(item.get("email", "")).strip() for item in invitees if item.get("email")]
    organizer_email = str((organizer or {}).get("email", "")).strip()
    if organizer_email and organizer_email not in emails:
        emails.insert(0, organizer_email)
    if len(emails) <= 4:
        return "; ".join(emails)
    return "; ".join(emails[:4]) + f" (+{len(emails) - 4})"


def extract_emails(meeting: Dict[str, object]) -> List[str]:
    """Collect participant and organizer emails for rule matching."""
    invitees = meeting.get("invitees", []) or []
    emails = set()
    for invitee in invitees:
        email = str(invitee.get("email", "")).strip().lower()
        if "@" in email:
            emails.add(email)

    organizer = meeting.get("organizer", {}) or {}
    organizer_email = str(organizer.get("email", "")).strip().lower()
    if "@" in organizer_email:
        emails.add(organizer_email)

    return sorted(emails)


def classify_meeting(meeting: Dict[str, object], rules: Dict[str, object]) -> Tuple[List[str], str, str]:
    """
    Classify a meeting using progressively weaker signals.

    Pass order:
        1. Manual override by meeting ID
        2. [Project] bracket prefix in the meeting title
        3. Exact meeting name rule
        4. Keyword rule in the meeting title
        5. Specific participant email
        6. Participant email domain
    """
    name = str(meeting.get("name", ""))
    meeting_id = str(meeting.get("id", ""))
    emails = extract_emails(meeting)
    domains = sorted({email.split("@", 1)[1] for email in emails if "@" in email})

    manual_overrides = rules.get("manual_overrides", {})
    if meeting_id in manual_overrides:
        return manual_overrides[meeting_id], "high", "manual_override"

    prefix = extract_bracket_prefix(name)
    if prefix:
        canonical = normalize_project_name(prefix, rules.get("project_aliases", {}))
        return [canonical], "high", "bracket_prefix"

    exact_name_rules = rules.get("exact_name_rules", {})
    if name in exact_name_rules:
        return exact_name_rules[name], "high", "exact_name"

    keyword_projects = []
    keyword_methods = []
    keyword_confidence = "high"
    for rule in rules.get("keyword_rules", []):
        if re.search(re.escape(rule["pattern"]), name, re.IGNORECASE):
            for project in rule["projects"]:
                if project not in keyword_projects:
                    keyword_projects.append(project)
            keyword_methods.append("keyword:" + rule["pattern"])
            keyword_confidence = min_confidence(keyword_confidence, rule.get("confidence", "high"))
    if keyword_projects:
        return keyword_projects, keyword_confidence, " | ".join(keyword_methods)

    for rule in rules.get("participant_email_rules", []):
        matched = sorted(set(emails) & set(rule["emails"]))
        if matched:
            return rule["projects"], rule.get("confidence", "medium"), "participant_email:" + matched[0]

    for rule in rules.get("participant_domain_rules", []):
        if rule["domain"] in domains:
            return rule["projects"], rule.get("confidence", "low"), "participant_domain:" + rule["domain"]

    return [], "unclassified", "unclassified"


def min_confidence(left: str, right: str) -> str:
    """Return the weaker of two confidence levels."""
    order = {"high": 0, "medium": 1, "low": 2, "manual": 3, "unclassified": 4}
    return left if order.get(left, 99) >= order.get(right, 99) else right


def classify_all(meetings: List[Dict[str, object]], rules: Dict[str, object]) -> List[Dict[str, object]]:
    """
    Classify all meetings and then propagate likely projects across shared conference IDs.

    Conference-ID propagation is intentionally conservative: it only copies the
    most common high-confidence classification within the same conference group.
    """
    conference_groups = defaultdict(list)
    for meeting in meetings:
        conference_id = str((meeting.get("extraProperties", {}) or {}).get("conferenceId", "")).strip()
        if conference_id:
            conference_groups[conference_id].append(str(meeting.get("id", "")))

    results = []
    by_id = {}

    for meeting in meetings:
        projects, confidence, method = classify_meeting(meeting, rules)
        result = {
            "id": str(meeting.get("id", "")),
            "name": str(meeting.get("name", "")),
            "date": format_date(str(meeting.get("happenedAt", ""))),
            "duration_min": round(float(meeting.get("duration", 0) or 0) / 60),
            "participants": format_participants(meeting.get("invitees", []) or [], meeting.get("organizer")),
            "participant_count": len(meeting.get("invitees", []) or []),
            "projects_auto": ", ".join(projects),
            "confidence": confidence,
            "method": method,
            "project_manual": "",
            "conference_id": str((meeting.get("extraProperties", {}) or {}).get("conferenceId", "")),
            "meeting_url": str(meeting.get("url", "")),
        }
        results.append(result)
        by_id[result["id"]] = result

    inferred_projects = {}
    for conference_id, meeting_ids in conference_groups.items():
        high_confidence_projects = [
            by_id[meeting_id]["projects_auto"]
            for meeting_id in meeting_ids
            if meeting_id in by_id
            and by_id[meeting_id]["confidence"] == "high"
            and by_id[meeting_id]["projects_auto"]
        ]
        if not high_confidence_projects:
            continue

        project_counter = Counter()
        for project_string in high_confidence_projects:
            for project in split_project_values(project_string):
                project_counter[project] += 1
        if project_counter:
            inferred_projects[conference_id] = project_counter.most_common(1)[0][0]

    for result in results:
        if result["confidence"] != "unclassified" or not result["conference_id"]:
            continue
        inferred = inferred_projects.get(result["conference_id"])
        if not inferred:
            continue
        result["projects_auto"] = inferred
        result["confidence"] = "medium"
        result["method"] = "conference_inference"

    return results


def split_project_values(value: str) -> List[str]:
    """Split project lists from CSV cells while accepting a few common separators."""
    return [part.strip() for part in re.split(r"[|,;]+", value or "") if part.strip()]


def absorb_manual_reviews(
    results: List[Dict[str, object]],
    reviewed_csv_path: Path,
    rules: Dict[str, object],
    rules_path: Path,
    apply_suggestions: bool,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """
    Read manual review decisions from a CSV and optionally turn repeated decisions
    into reusable exact-name rules.
    """
    reviewed = {}
    with open(reviewed_csv_path, encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            manual_project = (
                row.get("project_manual")
                or row.get("projeto_manual")
                or ""
            ).strip()
            if not manual_project:
                continue
            row_id = (row.get("id") or row.get("\ufeffid") or "").strip()
            if row_id:
                reviewed[row_id] = manual_project

    if not reviewed:
        print("No manual classifications were found in the reviewed CSV.")
        return results, rules

    print("\nFound {} manual classifications.".format(len(reviewed)))

    name_to_projects = defaultdict(list)
    for result in results:
        if result["id"] in reviewed:
            name_to_projects[result["name"]].append(reviewed[result["id"]])

    suggested_rules = {}
    for meeting_name, reviewed_projects in name_to_projects.items():
        if len(reviewed_projects) < 2:
            continue
        most_common_project = Counter(reviewed_projects).most_common(1)[0][0]
        if meeting_name not in rules["exact_name_rules"]:
            suggested_rules[meeting_name] = [most_common_project]

    if suggested_rules:
        print("\nSuggested exact-name rules:")
        for meeting_name, project_list in list(sorted(suggested_rules.items()))[:10]:
            print("  {!r} -> {!r}".format(meeting_name, ", ".join(project_list)))
        should_apply = apply_suggestions
        if not apply_suggestions:
            answer = input("\nAdd these rules to the rules file? [y/N]: ").strip().lower()
            should_apply = answer == "y"
        if should_apply:
            rules["exact_name_rules"].update(suggested_rules)
            save_rules(rules_path, rules)
            print("Saved {} new exact-name rules.".format(len(suggested_rules)))

    for result in results:
        if result["id"] in reviewed:
            result["project_manual"] = reviewed[result["id"]]
            if result["confidence"] == "unclassified":
                result["confidence"] = "manual"
                result["method"] = "manual_review"

    return results, rules


def write_csv(rows: List[Dict[str, object]], path: Path, columns: List[str]) -> None:
    """Write CSV output in UTF-8 with BOM for smooth spreadsheet imports."""
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print("  Saved: {} ({} rows)".format(path, len(rows)))


def write_stats(results: List[Dict[str, object]], path: Path) -> None:
    """Write a readable text summary that helps tune rules between runs."""
    total = len(results)
    by_confidence = Counter(result["confidence"] for result in results)
    by_method = Counter(result["method"] for result in results)
    by_project = Counter()

    for result in results:
        project_string = result.get("project_manual") or result.get("projects_auto", "")
        for project in split_project_values(project_string):
            by_project[project] += 1

    lines = [
        "=" * 60,
        "MEETING CLASSIFICATION SUMMARY",
        "=" * 60,
        "Total meetings: {}".format(total),
        "",
        "By confidence:",
    ]

    for confidence, count in sorted(by_confidence.items(), key=lambda item: (-item[1], item[0])):
        percentage = (count / total * 100) if total else 0
        lines.append("  {:<20} {:>5} ({:.1f}%)".format(confidence, count, percentage))

    lines.extend(["", "By method:"])
    for method, count in by_method.most_common():
        lines.append("  {:<35} {:>5}".format(method, count))

    lines.extend(["", "By project:"])
    for project, count in by_project.most_common():
        lines.append("  {:<35} {:>5}".format(project, count))

    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.write("\n")

    print(text)
    print("\n  Saved: {}".format(path))


def ensure_meetings_list(raw_meetings: object) -> List[Dict[str, object]]:
    """Validate that the input JSON is a list of meetings."""
    if not isinstance(raw_meetings, list):
        raise ValueError("Meetings JSON must contain a top-level list.")
    return raw_meetings


def main() -> None:
    args = parse_args()
    meetings_path = Path(args.meetings)
    rules_path = Path(args.rules)
    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not meetings_path.exists():
        print("Error: meetings file not found at {}".format(meetings_path))
        print("Generate it first with:")
        print("  python3 tldv_export.py")
        sys.exit(1)

    print("Loading meetings...")
    meetings = ensure_meetings_list(load_json(meetings_path))
    rules = load_rules(rules_path)
    print("  Loaded {} meetings".format(len(meetings)))

    print("\nClassifying meetings...")
    results = classify_all(meetings, rules)

    if args.absorb:
        reviewed_csv = Path(args.absorb)
        if not reviewed_csv.exists():
            print("Error: reviewed CSV not found at {}".format(reviewed_csv))
            sys.exit(1)
        print("\nAbsorbing manual reviews from {}".format(reviewed_csv))
        results, rules = absorb_manual_reviews(
            results,
            reviewed_csv,
            rules,
            rules_path,
            args.apply_suggestions,
        )

    results_sorted = sorted(results, key=lambda row: (row.get("date", ""), row.get("name", "").lower()))
    review_queue = [
        row
        for row in results_sorted
        if row["confidence"] in ("unclassified", "low") and not row["project_manual"]
    ]

    print("\nWriting outputs...")
    write_csv(results_sorted, output_dir / "classified_meetings.csv", FULL_OUTPUT_COLUMNS)
    write_csv(review_queue, output_dir / "review_queue.csv", REVIEW_QUEUE_COLUMNS)
    write_stats(results_sorted, output_dir / "classification_stats.txt")

    print(
        "\nNext steps:\n"
        "  1. Review review_queue.csv in a spreadsheet\n"
        "  2. Fill project_manual where needed\n"
        "  3. Re-run with --absorb to fold repeated patterns back into the rules"
    )


if __name__ == "__main__":
    main()
