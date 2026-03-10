"""Sync Canvas LMS assignments into a Notion database."""

import logging
import os
import re
from html import unescape

import requests
from notion_client import Client as NotionClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)

CANVAS_BASE = "https://canvas.eee.uci.edu/api/v1"
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["DATABASE_ID"]

notion = NotionClient(auth=NOTION_TOKEN)


# ---------------------------------------------------------------------------
# Canvas helpers
# ---------------------------------------------------------------------------

def fetch_assignments() -> list[dict]:
    """Return every assignment across all active courses for the authenticated user.

    Canvas has no single "all assignments" endpoint, so we first list
    courses and then paginate through each course's assignments.
    """
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    courses = _paginated_get(
        f"{CANVAS_BASE}/courses?enrollment_state=active",
        headers
    )    log.info("Found %d course(s) in Canvas.", len(courses))

    assignments: list[dict] = []
    for course in courses:
        course_id = course["id"]
        url = f"{CANVAS_BASE}/courses/{course_id}/assignments"
        try:
            course_assignments = _paginated_get(url, headers)
        except requests.HTTPError as exc:
            log.warning("Skipping course %s: %s", course_id, exc)
            continue
        assignments.extend(course_assignments)

    return assignments


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

def fetch_existing_notion_assignments() -> set[int]:
    """Return a set of Canvas Assignment IDs already present in Notion."""
    existing: set[int] = set()
    start_cursor: str | None = None

    while True:
        query_kwargs: dict = {
            "database_id": DATABASE_ID,
            "page_size": 100,
        }
        if start_cursor:
            query_kwargs["start_cursor"] = start_cursor

        result = notion.databases.query(**query_kwargs)

        for page in result["results"]:
            prop = page["properties"].get("Assignment ID", {})
            aid = prop.get("number")
            if aid is not None:
                existing.add(int(aid))

        if not result.get("has_more"):
            break
        start_cursor = result.get("next_cursor")

    return existing


def create_notion_assignment(assignment: dict) -> None:
    """Create a Notion page for a single Canvas assignment."""
    properties: dict = {
        "Name": {"title": [{"text": {"content": assignment.get("name", "Untitled")}}]},
        "Assignment ID": {"number": assignment["id"]},
        "Course ID": {"number": assignment.get("course_id")},
        "Points": {"number": assignment.get("points_possible")},
        "Canvas URL": {"url": assignment.get("html_url")},
    }

    due = assignment.get("due_at")
    if due:
        properties["Due Date"] = {"date": {"start": due}}

    children = _description_blocks(assignment.get("description") or "")

    notion.pages.create(
        parent={"database_id": DATABASE_ID},
        properties=properties,
        children=children,
    )


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
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Fetching assignments from Canvas …")
    assignments = fetch_assignments()
    log.info("Fetched %d assignment(s) from Canvas.", len(assignments))

    existing_ids = fetch_existing_notion_assignments()
    log.info("Found %d existing assignment(s) in Notion.", len(existing_ids))

    created = 0
    skipped = 0

    for a in assignments:
        if a["id"] in existing_ids:
            skipped += 1
            continue
        create_notion_assignment(a)
        created += 1

    log.info("Created %d assignment(s) in Notion.", created)
    log.info("Skipped %d duplicate(s).", skipped)


if __name__ == "__main__":
    main()
