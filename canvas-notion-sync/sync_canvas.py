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
    """Fetch upcoming assignments using Canvas planner API."""
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}

    url = f"{CANVAS_BASE}/planner/items"
    params = {
        "per_page": 100,
        "start_date": datetime.now(timezone.utc).isoformat(),
    }

    items = _paginated_get(url, headers, params)

    assignments = []
    course_names = {}

    for item in items:
        if item.get("plannable_type") != "assignment":
            continue

        assignment = item.get("plannable") or {}
        assignment["submission"] = item.get("submission")

        course_id = item.get("course_id")
        assignment["course_id"] = course_id
        assignment["due_at"] = item.get("plannable_date")

        course_names[course_id] = item.get("context_name", "Unknown")

        assignments.append(assignment)

    log.info("Fetched %d assignment(s) from planner.", len(assignments))

    return assignments, course_names


def _paginated_get(url: str, headers: dict, params: dict | None = None) -> list[dict]:
    """GET every page of a Canvas list endpoint."""
    results = []

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()

        results.extend(resp.json())

        url = _next_link(resp.headers.get("Link", ""))
        params = None

    return results


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            match = re.search(r"<([^>]+)>", part)
            if match:
                return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def _fetch_existing_by_assignment_id(database_id: str) -> dict[int, dict]:
    existing = {}
    start_cursor = None

    while True:
        kwargs = {"database_id": database_id, "page_size": 100}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor

        result = notion.databases.query(**kwargs)

        for page in result["results"]:
            props = page["properties"]
            aid = props.get("Assignment ID", {}).get("number")
            if aid is not None:
                existing[int(aid)] = {"page_id": page["id"]}

        if not result.get("has_more"):
            break

        start_cursor = result.get("next_cursor")

    return existing


def _fetch_existing_by_title(database_id: str) -> dict[str, dict]:
    existing = {}
    start_cursor = None

    while True:
        kwargs = {"database_id": database_id, "page_size": 100}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor

        result = notion.databases.query(**kwargs)

        for page in result["results"]:
            props = page["properties"]

            title_parts = props.get("Task name", {}).get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_parts)

            if not title:
                continue

            due = props.get("Due date", {}).get("date") or {}
            priority = props.get("Priority", {}).get("select") or {}
            effort = props.get("Effort level", {}).get("select") or {}

            existing[title] = {
                "page_id": page["id"],
                "due": due.get("start"),
                "priority": priority.get("name"),
                "effort": effort.get("name"),
            }

        if not result.get("has_more"):
            break

        start_cursor = result.get("next_cursor")

    return existing


# ---------------------------------------------------------------------------
# Logic helpers
# ---------------------------------------------------------------------------

def is_completed(assignment: dict) -> bool:
    submission = assignment.get("submission") or {}
    state = submission.get("workflow_state")
    return state in {"submitted", "graded"}


def days_until_due(due_at: str | None) -> float:
    if not due_at:
        return 1

    try:
        due_dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        delta = (due_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return max(delta, 1)
    except Exception:
        return 1


def determine_priority(due_at: str | None) -> str:
    days = days_until_due(due_at)

    if days <= 3:
        return "High"
    if days <= 7:
        return "Medium"
    return "Low"


def determine_effort(points: float) -> str:
    pts = points or 0

    if pts >= 80:
        return "Large"
    if pts >= 30:
        return "Medium"
    return "Small"


def determine_task_type(name: str) -> str:
    if re.search(r"\b(exam|midterm|final)\b", name, re.IGNORECASE):
        return "Exam"
    return "Homework"


# ---------------------------------------------------------------------------
# Property builders
# ---------------------------------------------------------------------------

def _build_assignment_properties(assignment: dict, course_names: dict) -> dict:
    course = course_names.get(assignment.get("course_id"), "Unknown")

    title = f"[{course}] {assignment.get('name','Untitled')}"

    props = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Assignment ID": {"number": assignment["id"]},
        "Course ID": {"number": assignment.get("course_id")},
        "Points": {"number": assignment.get("points_possible")},
        "Canvas URL": {"url": assignment.get("html_url")},
    }

    due = assignment.get("due_at")
    if due:
        props["Due Date"] = {"date": {"start": due}}

    return props


def _build_task_properties(assignment: dict, course_names: dict) -> dict:
    course = course_names.get(assignment.get("course_id"), "Unknown")

    title = f"[{course}] {assignment.get('name','Untitled')}"

    due = assignment.get("due_at")
    points = assignment.get("points_possible") or 0
    name = assignment.get("name","")

    status = "Done" if is_completed(assignment) else "Not started"

    return {
        "Task name": {"title": [{"text": {"content": title}}]},
        "Due date": {"date": {"start": due} if due else None},
        "Effort level": {"select": {"name": determine_effort(points)}},
        "Priority": {"select": {"name": determine_priority(due)}},
        "Status": {"status": {"name": status}},
        "Task type": {"select": {"name": determine_task_type(name)}},
    }


# ---------------------------------------------------------------------------
# Page creation
# ---------------------------------------------------------------------------

def create_assignment_page(a, course_names):
    notion.pages.create(
        parent={"database_id": ASSIGNMENTS_DB_ID},
        properties=_build_assignment_properties(a, course_names),
    )


def create_task_page(a, course_names):
    notion.pages.create(
        parent={"database_id": TASKS_DB_ID},
        properties=_build_task_properties(a, course_names),
    )


def update_task_page(page_id, a, course_names):
    props = _build_task_properties(a, course_names)

    notion.pages.update(
        page_id=page_id,
        properties={
            "Task name": props["Task name"],
            "Due date": props["Due date"],
            "Priority": props["Priority"],
            "Effort level": props["Effort level"],
            "Task type": props["Task type"],
            "Status": props["Status"],
        },
    )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _task_title(a, course_names):
    course = course_names.get(a.get("course_id"), "Unknown")
    return f"[{course}] {a.get('name','Untitled')}"


def _needs_update(existing, assignment):
    due = assignment.get("due_at")
    points = assignment.get("points_possible") or 0

    if (existing.get("due") or None) != (due or None):
        return True

    if existing.get("priority") != determine_priority(due):
        return True

    if existing.get("effort") != determine_effort(points):
        return True

    return False


def _sync_assignments_db(assignments, course_names):
    log.info("--- Assignments DB ---")

    existing = _fetch_existing_by_assignment_id(ASSIGNMENTS_DB_ID)

    created = skipped = 0

    for a in assignments:
        aid = a["id"]

        if aid in existing:
            skipped += 1
            continue

        create_assignment_page(a, course_names)
        created += 1

    log.info("Created %d | Skipped %d", created, skipped)


def _sync_tasks_db(assignments, course_names):
    log.info("--- Task Tracker DB ---")

    existing = _fetch_existing_by_title(TASKS_DB_ID)

    created = updated = skipped = 0

    for a in assignments:
        title = _task_title(a, course_names)

        if title in existing:
            if _needs_update(existing[title], a):
                update_task_page(existing[title]["page_id"], a, course_names)
                updated += 1
            else:
                skipped += 1
        else:
            create_task_page(a, course_names)
            created += 1

    log.info(
        "Created %d | Updated %d | Skipped %d",
        created,
        updated,
        skipped,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Fetching assignments from Canvas …")

    assignments, course_names = fetch_assignments()

    log.info("Processing %d assignments.", len(assignments))

    _sync_assignments_db(assignments, course_names)
    _sync_tasks_db(assignments, course_names)


if __name__ == "__main__":
    main()