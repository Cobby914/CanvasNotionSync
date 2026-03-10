"""Sync Canvas LMS assignments into two Notion databases."""

import logging
import os
import re
from datetime import datetime, timezone
from html import unescape

import requests
from notion_client import Client as NotionClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)

CANVAS_BASE = "https://canvas.eee.uci.edu/api/v1"
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
ASSIGNMENTS_DB_ID = os.environ["ASSIGNMENTS_DB_ID"]
TASKS_DB_ID = os.environ["TASKS_DB_ID"]

notion = NotionClient(auth=NOTION_TOKEN)


# ---------------------------------------------------------------------------
# Canvas helpers
# ---------------------------------------------------------------------------

def fetch_assignments() -> tuple[list[dict], dict[int, str]]:
    """Return all assignments and a course_id → course_name mapping."""
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    raw_courses = _paginated_get(
        f"{CANVAS_BASE}/courses?enrollment_state=active",
        headers,
    )
    log.info("Found %d course(s) in Canvas.", len(raw_courses))

    course_names: dict[int, str] = {
        c["id"]: c.get("name", "Unknown Course") for c in raw_courses
    }

    assignments: list[dict] = []
    for course in raw_courses:
        course_id = course["id"]
        url = f"{CANVAS_BASE}/courses/{course_id}/assignments"
        try:
            course_assignments = _paginated_get(url, headers)
        except requests.HTTPError as exc:
            log.warning("Skipping course %s: %s", course_id, exc)
            continue
        assignments.extend(course_assignments)

    return assignments, course_names


def _paginated_get(url: str, headers: dict) -> list[dict]:
    """GET every page of a Canvas list endpoint (per_page=100)."""
    params: dict = {"per_page": 100}
    results: list[dict] = []

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        results.extend(resp.json())
        url = _next_link(resp.headers.get("Link", ""))
        params = {}

    return results


def _next_link(link_header: str) -> str | None:
    """Parse the ``next`` URL from a Canvas ``Link`` header."""
    for part in link_header.split(","):
        if 'rel="next"' in part:
            match = re.search(r"<([^>]+)>", part)
            if match:
                return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Notion — shared helpers
# ---------------------------------------------------------------------------

def _fetch_existing_by_assignment_id(database_id: str) -> dict[int, dict]:
    """Return a mapping of Assignment ID → page metadata for the Assignments DB."""
    existing: dict[int, dict] = {}
    start_cursor: str | None = None

    while True:
        query_kwargs: dict = {"database_id": database_id, "page_size": 100}
        if start_cursor:
            query_kwargs["start_cursor"] = start_cursor

        result = notion.databases.query(**query_kwargs)

        for page in result["results"]:
            props = page["properties"]
            aid = props.get("Assignment ID", {}).get("number")
            if aid is None:
                continue
            existing[int(aid)] = {"page_id": page["id"]}

        if not result.get("has_more"):
            break
        start_cursor = result.get("next_cursor")

    return existing


def _fetch_existing_by_title(database_id: str) -> dict[str, dict]:
    """Return a mapping of task title → page metadata for the Task Tracker DB."""
    existing: dict[str, dict] = {}
    start_cursor: str | None = None

    while True:
        query_kwargs: dict = {"database_id": database_id, "page_size": 100}
        if start_cursor:
            query_kwargs["start_cursor"] = start_cursor

        result = notion.databases.query(**query_kwargs)

        for page in result["results"]:
            props = page["properties"]
            title_parts = props.get("Task name", {}).get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_parts)
            if not title:
                continue

            due_prop = props.get("Due date", {}).get("date") or {}
            priority_prop = props.get("Priority", {}).get("select") or {}
            effort_prop = props.get("Effort level", {}).get("select") or {}

            existing[title] = {
                "page_id": page["id"],
                "due": due_prop.get("start"),
                "priority": priority_prop.get("name"),
                "effort": effort_prop.get("name"),
            }

        if not result.get("has_more"):
            break
        start_cursor = result.get("next_cursor")

    return existing


def _description_blocks(html: str) -> list[dict]:
    """Convert an HTML description into Notion paragraph blocks."""
    text = _strip_html(html).strip()
    if not text:
        return []

    blocks: list[dict] = []
    for paragraph in re.split(r"\n{2,}", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for chunk in _chunk(paragraph, 2000):
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                },
            })
    return blocks


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text)


def _chunk(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


# ---------------------------------------------------------------------------
# Assignments DB — raw Canvas data
# ---------------------------------------------------------------------------

def _build_assignment_properties(
    assignment: dict,
    course_names: dict[int, str],
) -> dict:
    course_name = course_names.get(assignment.get("course_id"), "Unknown")
    title = f"[{course_name}] {assignment.get('name', 'Untitled')}"

    properties: dict = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Assignment ID": {"number": assignment["id"]},
        "Course ID": {"number": assignment.get("course_id")},
        "Points": {"number": assignment.get("points_possible")},
        "Canvas URL": {"url": assignment.get("html_url")},
    }

    due = assignment.get("due_at")
    if due:
        properties["Due Date"] = {"date": {"start": due}}

    return properties


def create_assignment_page(assignment: dict, course_names: dict[int, str]) -> None:
    properties = _build_assignment_properties(assignment, course_names)
    children = _description_blocks(assignment.get("description") or "")

    notion.pages.create(
        parent={"database_id": ASSIGNMENTS_DB_ID},
        properties=properties,
        children=children[:100],
    )


# ---------------------------------------------------------------------------
# Task Tracker DB — enriched view matching your Notion template
#
# Columns: Task name | Assignee (skip) | Due date | Effort level |
#          Priority  | Status          | Task type
# ---------------------------------------------------------------------------

def determine_task_type(name: str) -> str:
    """'Exam' if the assignment name mentions exam/midterm/final, else 'Homework'."""
    if re.search(r"\b(exam|midterm|final)\b", name, re.IGNORECASE):
        return "Exam"
    return "Homework"


def days_until_due(due_at: str | None) -> float:
    """Days from now until the due date (minimum 1)."""
    if not due_at:
        return 1.0
    try:
        due_dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        delta = (due_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return max(delta, 1.0)
    except (ValueError, TypeError):
        return 1.0


def determine_priority(due_at: str | None) -> str:
    """Priority based purely on how close the due date is."""
    days = days_until_due(due_at)
    if days <= 3:
        return "High"
    if days <= 7:
        return "Medium"
    return "Low"


def determine_effort(points: float) -> str:
    """Effort level based on point value."""
    pts = points or 0
    if pts >= 80:
        return "Large"
    if pts >= 30:
        return "Medium"
    return "Small"


def _build_task_properties(
    assignment: dict,
    course_names: dict[int, str],
) -> dict:
    """Build properties matching the Task Tracker columns exactly."""
    course_name = course_names.get(assignment.get("course_id"), "Unknown")
    title = f"[{course_name}] {assignment.get('name', 'Untitled')}"

    due = assignment.get("due_at")
    points = assignment.get("points_possible") or 0
    name = assignment.get("name", "")

    properties: dict = {
        "Task name": {"title": [{"text": {"content": title}}]},
        "Due date": {"date": {"start": due} if due else None},
        "Effort level": {"select": {"name": determine_effort(points)}},
        "Priority": {"select": {"name": determine_priority(due)}},
        "Status": {"status": {"name": "Not started"}},
        "Task type": {"select": {"name": determine_task_type(name)}},
    }

    return properties


def create_task_page(assignment: dict, course_names: dict[int, str]) -> None:
    properties = _build_task_properties(assignment, course_names)
    notion.pages.create(parent={"database_id": TASKS_DB_ID}, properties=properties)


def update_task_page(
    page_id: str,
    assignment: dict,
    course_names: dict[int, str],
) -> None:
    """Update an existing task (preserves Status so manual changes aren't lost)."""
    props = _build_task_properties(assignment, course_names)

    update_props = {
        "Task name": props["Task name"],
        "Due date": props["Due date"],
        "Priority": props["Priority"],
        "Effort level": props["Effort level"],
        "Task type": props["Task type"],
    }

    notion.pages.update(page_id=page_id, properties=update_props)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _is_upcoming(assignment: dict, now: datetime) -> bool:
    due = assignment.get("due_at")
    if not due:
        return False
    try:
        due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
        return due_dt > now
    except (ValueError, TypeError):
        return False


def _needs_update(existing: dict, assignment: dict) -> bool:
    due = assignment.get("due_at")
    points = assignment.get("points_possible") or 0

    if (existing.get("due") or None) != (due or None):
        return True
    if existing.get("priority") != determine_priority(due):
        return True
    if existing.get("effort") != determine_effort(points):
        return True
    return False


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def _sync_assignments_db(
    assignments: list[dict],
    course_names: dict[int, str],
) -> None:
    log.info("--- Assignments DB ---")
    existing = _fetch_existing_by_assignment_id(ASSIGNMENTS_DB_ID)
    log.info("Found %d existing page(s).", len(existing))

    created = skipped = failed = 0
    for a in assignments:
        aid = a["id"]
        if aid in existing:
            skipped += 1
            continue
        try:
            create_assignment_page(a, course_names)
            created += 1
        except Exception:
            log.exception("Failed to create assignment %s (%s)", aid, a.get("name"))
            failed += 1

    log.info("Created %d | Skipped %d | Failed %d", created, skipped, failed)


def _task_title(assignment: dict, course_names: dict[int, str]) -> str:
    """Build the task title used for dedup in the Task Tracker DB."""
    course_name = course_names.get(assignment.get("course_id"), "Unknown")
    return f"[{course_name}] {assignment.get('name', 'Untitled')}"


def _sync_tasks_db(
    assignments: list[dict],
    course_names: dict[int, str],
) -> None:
    log.info("--- Task Tracker DB ---")
    existing = _fetch_existing_by_title(TASKS_DB_ID)
    log.info("Found %d existing page(s).", len(existing))

    created = updated = skipped = failed = 0
    for a in assignments:
        title = _task_title(a, course_names)
        try:
            if title in existing:
                if _needs_update(existing[title], a):
                    update_task_page(existing[title]["page_id"], a, course_names)
                    updated += 1
                else:
                    skipped += 1
            else:
                create_task_page(a, course_names)
                created += 1
        except Exception:
            log.exception("Failed to sync task %s (%s)", a["id"], a.get("name"))
            failed += 1

    log.info(
        "Created %d | Updated %d | Skipped %d | Failed %d",
        created, updated, skipped, failed,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Fetching assignments from Canvas …")
    all_assignments, course_names = fetch_assignments()
    log.info("Fetched %d total assignment(s) from Canvas.", len(all_assignments))

    now = datetime.now(timezone.utc)
    assignments = [a for a in all_assignments if _is_upcoming(a, now)]
    log.info("Filtered to %d upcoming assignment(s).", len(assignments))

    _sync_assignments_db(assignments, course_names)
    _sync_tasks_db(assignments, course_names)


if __name__ == "__main__":
    main()
