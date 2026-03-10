"""Sync Canvas LMS assignments into a Notion database."""

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
    """Return all assignments and a course_id → course_name mapping.

    Canvas has no single "all assignments" endpoint, so we first list
    courses and then paginate through each course's assignments.
    """
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
# Notion helpers
# ---------------------------------------------------------------------------

def _fetch_existing_ids(database_id: str) -> dict[int, dict]:
    """Return a mapping of Canvas Assignment ID → page metadata for a Notion DB."""
    existing: dict[int, dict] = {}
    start_cursor: str | None = None

    while True:
        query_kwargs: dict = {
            "database_id": database_id,
            "page_size": 100,
        }
        if start_cursor:
            query_kwargs["start_cursor"] = start_cursor

        result = notion.databases.query(**query_kwargs)

        for page in result["results"]:
            props = page["properties"]
            aid_prop = props.get("Assignment ID", {})
            aid = aid_prop.get("number")
            if aid is None:
                continue

            due_prop = props.get("Due Date", {}).get("date") or {}
            priority_prop = props.get("Priority", {}).get("select") or {}
            effort_prop = props.get("Effort Level", {}).get("select") or {}

            existing[int(aid)] = {
                "page_id": page["id"],
                "due": due_prop.get("start"),
                "priority": priority_prop.get("name"),
                "effort": effort_prop.get("name"),
            }

        if not result.get("has_more"):
            break
        start_cursor = result.get("next_cursor")

    return existing


def fetch_existing_notion_assignments() -> dict[int, dict]:
    """Return existing assignment IDs from the Assignments database."""
    return _fetch_existing_ids(ASSIGNMENTS_DB_ID)


def fetch_existing_notion_tasks() -> dict[int, dict]:
    """Return existing assignment IDs from the Task Tracker database."""
    return _fetch_existing_ids(TASKS_DB_ID)


# --- Assignments DB (raw) --------------------------------------------------

def _build_assignment_properties(
    assignment: dict,
    course_names: dict[int, str],
) -> dict:
    """Properties for the raw Assignments database."""
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
    """Create a page in the Assignments database."""
    properties = _build_assignment_properties(assignment, course_names)
    children = _description_blocks(assignment.get("description") or "")

    notion.pages.create(
        parent={"database_id": ASSIGNMENTS_DB_ID},
        properties=properties,
        children=children[:100],
    )


# --- Task Tracker DB (enriched) --------------------------------------------

def _build_task_properties(
    assignment: dict,
    course_names: dict[int, str],
) -> dict:
    """Properties for the Task Tracker database."""
    course_name = course_names.get(assignment.get("course_id"), "Unknown")
    title = f"[{course_name}] {assignment.get('name', 'Untitled')}"

    due = assignment.get("due_at")
    points = assignment.get("points_possible") or 0

    properties: dict = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Assignment ID": {"number": assignment["id"]},
        "Course ID": {"number": assignment.get("course_id")},
        "Points": {"number": assignment.get("points_possible")},
        "Canvas URL": {"url": assignment.get("html_url")},
        "Status": {"select": {"name": "Not started"}},
        "Task type": {"select": {"name": "Assignment"}},
        "Priority": {"select": {"name": determine_priority(points, due)}},
        "Effort Level": {"select": {"name": determine_effort(points)}},
    }

    if due:
        properties["Due Date"] = {"date": {"start": due}}

    return properties


def create_task_page(assignment: dict, course_names: dict[int, str]) -> None:
    """Create a page in the Task Tracker database."""
    properties = _build_task_properties(assignment, course_names)
    children = _description_blocks(assignment.get("description") or "")

    notion.pages.create(
        parent={"database_id": TASKS_DB_ID},
        properties=properties,
        children=children[:100],
    )


def update_task_page(
    page_id: str,
    assignment: dict,
    course_names: dict[int, str],
) -> None:
    """Update an existing Task Tracker page (preserves Status)."""
    props = _build_task_properties(assignment, course_names)

    update_props = {
        "Name": props["Name"],
        "Due Date": props.get("Due Date", {"date": None}),
        "Priority": props["Priority"],
        "Effort Level": props["Effort Level"],
        "Points": props["Points"],
    }

    notion.pages.update(page_id=page_id, properties=update_props)


def _description_blocks(html: str) -> list[dict]:
    """Convert an HTML description into Notion paragraph blocks.

    Strips tags and splits on double-newlines so the body is readable.
    Notion limits rich-text content to 2 000 characters per block.
    """
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
    """Naive but sufficient HTML → plain-text conversion."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text)


def _chunk(text: str, size: int) -> list[str]:
    """Split *text* into pieces of at most *size* characters."""
    return [text[i : i + size] for i in range(0, len(text), size)]


# ---------------------------------------------------------------------------
# Filtering & scoring
# ---------------------------------------------------------------------------

def _is_upcoming(assignment: dict, now: datetime) -> bool:
    """Return True if the assignment's due date is in the future.

    Assignments with no due date are excluded.
    """
    due = assignment.get("due_at")
    if not due:
        return False
    try:
        due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
        return due_dt > now
    except (ValueError, TypeError):
        return False


def days_until_due(due_at: str | None) -> float:
    """Return the number of days until the due date (minimum 1)."""
    if not due_at:
        return 1.0
    try:
        due_dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        delta = (due_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return max(delta, 1.0)
    except (ValueError, TypeError):
        return 1.0


def workload_score(points: float, due_at: str | None) -> float:
    """Return points / days_until_due."""
    return (points or 0) / days_until_due(due_at)


def determine_priority(points: float, due_at: str | None) -> str:
    """High / Medium / Low based on workload score."""
    score = workload_score(points, due_at)
    if score >= 20:
        return "High"
    if score >= 8:
        return "Medium"
    return "Low"


def determine_effort(points: float) -> str:
    """Large / Medium / Small based on point value."""
    pts = points or 0
    if pts >= 80:
        return "Large"
    if pts >= 30:
        return "Medium"
    return "Small"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _needs_update(existing: dict, assignment: dict) -> bool:
    """Return True if due date, priority, or effort changed."""
    due = assignment.get("due_at")
    points = assignment.get("points_possible") or 0

    if (existing.get("due") or None) != (due or None):
        return True
    if existing.get("priority") != determine_priority(points, due):
        return True
    if existing.get("effort") != determine_effort(points):
        return True
    return False


def _sync_assignments_db(
    assignments: list[dict],
    course_names: dict[int, str],
) -> None:
    """Sync upcoming assignments into the Assignments database (create only)."""
    log.info("--- Assignments DB ---")
    existing = fetch_existing_notion_assignments()
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


def _sync_tasks_db(
    assignments: list[dict],
    course_names: dict[int, str],
) -> None:
    """Sync upcoming assignments into the Task Tracker database (create + update)."""
    log.info("--- Task Tracker DB ---")
    existing = fetch_existing_notion_tasks()
    log.info("Found %d existing page(s).", len(existing))

    created = updated = skipped = failed = 0
    for a in assignments:
        aid = a["id"]
        try:
            if aid in existing:
                if _needs_update(existing[aid], a):
                    update_task_page(existing[aid]["page_id"], a, course_names)
                    updated += 1
                else:
                    skipped += 1
            else:
                create_task_page(a, course_names)
                created += 1
        except Exception:
            log.exception("Failed to sync task %s (%s)", aid, a.get("name"))
            failed += 1

    log.info(
        "Created %d | Updated %d | Skipped %d | Failed %d",
        created, updated, skipped, failed,
    )


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
