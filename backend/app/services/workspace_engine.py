"""
Zeni Workspace — engine service.

Responsibilities:
    - render_page_html(page_id)            -> HTML preview / export
    - export_markdown(page_id)             -> Markdown export
    - import_markdown(workspace_id, parent_id, md_content, author_email)
                                           -> create page + blocks from a .md doc
    - search(workspace_id, query)          -> full-text search across pages + blocks
                                              (uses ws_pages.search_tsv and
                                               ws_blocks.search_tsv with ts_rank)
    - take_snapshot(page_id, edited_by)    -> persist current page+blocks to
                                              ws_page_history

All functions are async and operate on an injected ``AsyncSession``.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.workspace.engine")


# ─────────────────────────────────────────────────────────
# Block type constants
# ─────────────────────────────────────────────────────────
BLOCK_TYPES: set[str] = {
    "paragraph", "heading1", "heading2", "heading3",
    "bulletlist", "numberlist", "todo", "toggle",
    "code", "table", "embed", "divider", "quote",
    "callout", "image", "file", "database",
}

DEFAULT_BLOCK_TYPE = "paragraph"
MAX_SEARCH_HITS = 50
MAX_SEARCH_QUERY_LEN = 200
HISTORY_RETAIN = 50  # most recent snapshots kept


# ─────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────
def _esc(s: str | None) -> str:
    return html_lib.escape(s or "", quote=True)


def _safe_jsonb(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _sanitize_query(q: str) -> str:
    """Sanitize a user query for plainto_tsquery — strip non-word noise."""
    q = (q or "").strip()
    if not q:
        return ""
    if len(q) > MAX_SEARCH_QUERY_LEN:
        q = q[:MAX_SEARCH_QUERY_LEN]
    # plainto_tsquery handles raw tokens, but trim weird chars to be safe
    return re.sub(r"[\x00-\x1f]", " ", q)


# ─────────────────────────────────────────────────────────
# 1. Render to HTML
# ─────────────────────────────────────────────────────────
async def render_page_html(db: AsyncSession, page_id: int) -> str:
    """Return a single HTML document representing the page + its blocks.

    Used for export and quick preview. Output is intentionally simple —
    a wrapping <article> with one element per block. Caller can style.
    """
    page = (await db.execute(text("""
        SELECT id, title, icon, cover_url, is_archived, created_at, updated_at
          FROM ws_pages
         WHERE id = :id
    """), {"id": page_id})).first()
    if not page:
        return "<article><h1>Page not found</h1></article>"

    blocks = (await db.execute(text("""
        SELECT id, type, content, properties, position, parent_block_id
          FROM ws_blocks
         WHERE page_id = :pid
         ORDER BY position ASC, id ASC
    """), {"pid": page_id})).all()

    parts: list[str] = ["<article class=\"ws-page\">"]
    if page[2]:
        parts.append(f"<div class=\"ws-icon\">{_esc(page[2])}</div>")
    if page[3]:
        parts.append(f"<img class=\"ws-cover\" src=\"{_esc(page[3])}\" alt=\"\"/>")
    parts.append(f"<h1 class=\"ws-title\">{_esc(page[1])}</h1>")

    for b in blocks:
        parts.append(_render_block_html(b))
    parts.append("</article>")
    return "\n".join(parts)


def _render_block_html(b) -> str:
    btype = (b[1] or "paragraph").lower()
    content = b[2] or ""
    props = _safe_jsonb(b[3]) or {}
    if not isinstance(props, dict):
        props = {}

    if btype == "heading1":
        return f"<h1>{_esc(content)}</h1>"
    if btype == "heading2":
        return f"<h2>{_esc(content)}</h2>"
    if btype == "heading3":
        return f"<h3>{_esc(content)}</h3>"
    if btype == "bulletlist":
        return f"<ul><li>{_esc(content)}</li></ul>"
    if btype == "numberlist":
        return f"<ol><li>{_esc(content)}</li></ol>"
    if btype == "todo":
        checked = "checked" if props.get("checked") else ""
        return (f"<div class=\"ws-todo\">"
                f"<input type=\"checkbox\" disabled {checked}/>"
                f"<span>{_esc(content)}</span></div>")
    if btype == "toggle":
        return (f"<details><summary>{_esc(content)}</summary>"
                f"<div class=\"ws-toggle-body\"></div></details>")
    if btype == "code":
        lang = _esc(str(props.get("language") or ""))
        return (f"<pre class=\"ws-code\" data-lang=\"{lang}\">"
                f"<code>{_esc(content)}</code></pre>")
    if btype == "quote":
        return f"<blockquote>{_esc(content)}</blockquote>"
    if btype == "callout":
        emoji = _esc(str(props.get("emoji") or "💡"))
        return (f"<div class=\"ws-callout\"><span>{emoji}</span>"
                f"<span>{_esc(content)}</span></div>")
    if btype == "divider":
        return "<hr/>"
    if btype == "image":
        url = _esc(str(props.get("url") or content))
        alt = _esc(str(props.get("alt") or ""))
        return f"<img class=\"ws-img\" src=\"{url}\" alt=\"{alt}\"/>"
    if btype == "file":
        url = _esc(str(props.get("url") or content))
        name = _esc(str(props.get("name") or "file"))
        return f"<a class=\"ws-file\" href=\"{url}\" download>{name}</a>"
    if btype == "embed":
        url = _esc(str(props.get("url") or content))
        return (f"<div class=\"ws-embed\"><a href=\"{url}\" "
                f"target=\"_blank\" rel=\"noopener\">{url}</a></div>")
    if btype == "table":
        rows = props.get("rows") or []
        if not isinstance(rows, list):
            rows = []
        out = ["<table class=\"ws-table\"><tbody>"]
        for row in rows:
            cells = row if isinstance(row, list) else []
            out.append("<tr>" + "".join(
                f"<td>{_esc(str(c))}</td>" for c in cells
            ) + "</tr>")
        out.append("</tbody></table>")
        return "".join(out)
    if btype == "database":
        name = _esc(str(props.get("name") or "Database"))
        return f"<div class=\"ws-db-ref\" data-db-id=\"{_esc(str(props.get('database_id') or ''))}\">📊 {name}</div>"
    # default — paragraph
    return f"<p>{_esc(content)}</p>"


# ─────────────────────────────────────────────────────────
# 2. Export Markdown
# ─────────────────────────────────────────────────────────
async def export_markdown(db: AsyncSession, page_id: int) -> str:
    page = (await db.execute(text("""
        SELECT id, title, icon FROM ws_pages WHERE id = :id
    """), {"id": page_id})).first()
    if not page:
        return "# Page not found\n"

    blocks = (await db.execute(text("""
        SELECT id, type, content, properties
          FROM ws_blocks
         WHERE page_id = :pid
         ORDER BY position ASC, id ASC
    """), {"pid": page_id})).all()

    out: list[str] = []
    icon = page[2] or ""
    out.append(f"# {icon + ' ' if icon else ''}{page[1] or 'Untitled'}\n")
    for b in blocks:
        out.append(_render_block_md(b))
    return "\n".join(out).rstrip() + "\n"


def _render_block_md(b) -> str:
    btype = (b[1] or "paragraph").lower()
    content = b[2] or ""
    props = _safe_jsonb(b[3]) or {}
    if not isinstance(props, dict):
        props = {}

    if btype == "heading1":
        return f"\n# {content}\n"
    if btype == "heading2":
        return f"\n## {content}\n"
    if btype == "heading3":
        return f"\n### {content}\n"
    if btype == "bulletlist":
        return f"- {content}"
    if btype == "numberlist":
        return f"1. {content}"
    if btype == "todo":
        mark = "x" if props.get("checked") else " "
        return f"- [{mark}] {content}"
    if btype == "toggle":
        return f"\n<details><summary>{content}</summary></details>\n"
    if btype == "code":
        lang = str(props.get("language") or "")
        return f"\n```{lang}\n{content}\n```\n"
    if btype == "quote":
        return "\n" + "\n".join(f"> {ln}" for ln in (content or "").splitlines()) + "\n"
    if btype == "callout":
        emoji = props.get("emoji") or "💡"
        return f"\n> {emoji} {content}\n"
    if btype == "divider":
        return "\n---\n"
    if btype == "image":
        url = props.get("url") or content
        alt = props.get("alt") or ""
        return f"![{alt}]({url})"
    if btype == "file":
        url = props.get("url") or content
        name = props.get("name") or "file"
        return f"[{name}]({url})"
    if btype == "embed":
        url = props.get("url") or content
        return f"<{url}>"
    if btype == "table":
        rows = props.get("rows") or []
        if not isinstance(rows, list) or not rows:
            return ""
        head = rows[0] if isinstance(rows[0], list) else []
        body = [r for r in rows[1:] if isinstance(r, list)]
        if not head:
            return ""
        lines = ["| " + " | ".join(str(c) for c in head) + " |",
                 "| " + " | ".join(["---"] * len(head)) + " |"]
        for r in body:
            lines.append("| " + " | ".join(str(c) for c in r) + " |")
        return "\n".join(lines)
    if btype == "database":
        return f"\n[Database: {props.get('name') or props.get('database_id') or ''}]\n"
    # paragraph
    return content


# ─────────────────────────────────────────────────────────
# 3. Import Markdown
# ─────────────────────────────────────────────────────────
_MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_MD_BULLET_RE = re.compile(r"^[\-\*]\s+(.+)$")
_MD_NUMBER_RE = re.compile(r"^\d+\.\s+(.+)$")
_MD_TODO_RE = re.compile(r"^[\-\*]\s+\[(x| )\]\s+(.+)$", re.IGNORECASE)
_MD_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_MD_DIVIDER_RE = re.compile(r"^(?:-{3,}|\*{3,})$")


async def import_markdown(
    db: AsyncSession,
    *,
    workspace_id: str,
    parent_id: int | None,
    md_content: str,
    author_email: str | None,
    title: str | None = None,
) -> int:
    """Create a new page (under parent_id) and parse markdown into blocks.

    Returns the new page id.
    """
    if md_content is None:
        md_content = ""
    lines = md_content.replace("\r\n", "\n").split("\n")

    derived_title = title or "Imported"
    body_start = 0
    if lines:
        m = _MD_HEADING_RE.match(lines[0].strip())
        if m and len(m.group(1)) == 1:
            derived_title = m.group(2).strip() or derived_title
            body_start = 1

    page_row = (await db.execute(text("""
        INSERT INTO ws_pages (workspace_id, parent_id, title, created_by, updated_by)
        VALUES (:ws, :pid, :title, :cb, :cb)
        RETURNING id
    """), {
        "ws": workspace_id,
        "pid": parent_id,
        "title": derived_title[:500],
        "cb": author_email,
    })).first()
    page_id = int(page_row[0])

    blocks = _parse_markdown_to_blocks(lines[body_start:])
    if blocks:
        # insert in order with monotonic positions
        for idx, blk in enumerate(blocks):
            await db.execute(text("""
                INSERT INTO ws_blocks
                  (page_id, type, content, properties, position)
                VALUES
                  (:pid, :type, :content, :props::jsonb, :pos)
            """), {
                "pid": page_id,
                "type": blk["type"],
                "content": blk.get("content"),
                "props": json.dumps(blk.get("properties") or {}),
                "pos": float(idx + 1),
            })

    return page_id


def _parse_markdown_to_blocks(lines: list[str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        ln = raw.rstrip()

        # Skip pure blank lines
        if not ln.strip():
            i += 1
            continue

        # Code fence
        if ln.lstrip().startswith("```"):
            lang = ln.lstrip().lstrip("`").strip()
            buf: list[str] = []
            i += 1
            while i < n and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            blocks.append({
                "type": "code",
                "content": "\n".join(buf),
                "properties": {"language": lang or "plain"},
            })
            continue

        # Divider
        if _MD_DIVIDER_RE.match(ln.strip()):
            blocks.append({"type": "divider", "content": None, "properties": {}})
            i += 1
            continue

        # Heading
        h = _MD_HEADING_RE.match(ln)
        if h:
            level = len(h.group(1))
            level = min(level, 3)
            blocks.append({
                "type": f"heading{level}",
                "content": h.group(2).strip(),
                "properties": {},
            })
            i += 1
            continue

        # Todo
        t = _MD_TODO_RE.match(ln)
        if t:
            blocks.append({
                "type": "todo",
                "content": t.group(2).strip(),
                "properties": {"checked": t.group(1).lower() == "x"},
            })
            i += 1
            continue

        # Bullet
        b = _MD_BULLET_RE.match(ln)
        if b:
            blocks.append({
                "type": "bulletlist",
                "content": b.group(1).strip(),
                "properties": {},
            })
            i += 1
            continue

        # Number list
        nb = _MD_NUMBER_RE.match(ln)
        if nb:
            blocks.append({
                "type": "numberlist",
                "content": nb.group(1).strip(),
                "properties": {},
            })
            i += 1
            continue

        # Blockquote (collect contiguous)
        if _MD_QUOTE_RE.match(ln):
            buf2: list[str] = []
            while i < n and _MD_QUOTE_RE.match(lines[i].rstrip()):
                m = _MD_QUOTE_RE.match(lines[i].rstrip())
                buf2.append(m.group(1) if m else "")
                i += 1
            blocks.append({
                "type": "quote",
                "content": "\n".join(buf2).strip(),
                "properties": {},
            })
            continue

        # Default — paragraph (collect contiguous non-blank, non-special lines)
        buf3: list[str] = [ln]
        i += 1
        while i < n:
            nxt = lines[i].rstrip()
            if (not nxt.strip()
                    or _MD_HEADING_RE.match(nxt)
                    or _MD_BULLET_RE.match(nxt)
                    or _MD_NUMBER_RE.match(nxt)
                    or _MD_QUOTE_RE.match(nxt)
                    or _MD_DIVIDER_RE.match(nxt.strip())
                    or nxt.lstrip().startswith("```")):
                break
            buf3.append(nxt)
            i += 1
        blocks.append({
            "type": "paragraph",
            "content": " ".join(s.strip() for s in buf3).strip(),
            "properties": {},
        })
    return blocks


# ─────────────────────────────────────────────────────────
# 4. Search
# ─────────────────────────────────────────────────────────
async def search(
    db: AsyncSession,
    *,
    workspace_id: str,
    query: str,
    limit: int = MAX_SEARCH_HITS,
) -> dict[str, Any]:
    """Full-text search across pages and blocks of a workspace.

    Returns a dict { pages: [...], blocks: [...] } where each item carries
    a ``rank`` score and a snippet for blocks.
    """
    q = _sanitize_query(query)
    if not q:
        return {"workspace_id": workspace_id, "query": query,
                "pages": [], "blocks": []}

    n = max(1, min(int(limit), MAX_SEARCH_HITS))

    # Pages — match on title only; archived pages excluded.
    page_rows = (await db.execute(text("""
        SELECT id, title, icon, updated_at,
               ts_rank(search_tsv, plainto_tsquery('simple', :q)) AS rank
          FROM ws_pages
         WHERE workspace_id = :ws
           AND is_archived = FALSE
           AND search_tsv @@ plainto_tsquery('simple', :q)
         ORDER BY rank DESC, updated_at DESC
         LIMIT :lim
    """), {"ws": workspace_id, "q": q, "lim": n})).all()

    page_hits = [
        {
            "id": int(r[0]),
            "title": r[1],
            "icon": r[2],
            "updated_at": r[3].isoformat() if r[3] else None,
            "rank": float(r[4]) if r[4] is not None else 0.0,
        }
        for r in page_rows
    ]

    # Blocks — join pages to filter workspace + drop archived; produce a
    # short headline as snippet.
    block_rows = (await db.execute(text("""
        SELECT b.id, b.page_id, b.type,
               ts_headline('simple', COALESCE(b.content,''),
                           plainto_tsquery('simple', :q),
                           'StartSel=<mark>,StopSel=</mark>,MaxWords=18,MinWords=5')
                 AS snippet,
               ts_rank(b.search_tsv, plainto_tsquery('simple', :q)) AS rank,
               p.title AS page_title
          FROM ws_blocks b
          JOIN ws_pages p ON p.id = b.page_id
         WHERE p.workspace_id = :ws
           AND p.is_archived = FALSE
           AND b.search_tsv @@ plainto_tsquery('simple', :q)
         ORDER BY rank DESC
         LIMIT :lim
    """), {"ws": workspace_id, "q": q, "lim": n})).all()

    block_hits = [
        {
            "id": int(r[0]),
            "page_id": int(r[1]),
            "type": r[2],
            "snippet": r[3],
            "rank": float(r[4]) if r[4] is not None else 0.0,
            "page_title": r[5],
        }
        for r in block_rows
    ]

    return {
        "workspace_id": workspace_id,
        "query": query,
        "pages": page_hits,
        "blocks": block_hits,
        "total": len(page_hits) + len(block_hits),
    }


# ─────────────────────────────────────────────────────────
# 5. Snapshots / version history
# ─────────────────────────────────────────────────────────
async def take_snapshot(
    db: AsyncSession,
    *,
    page_id: int,
    edited_by: str | None,
    note: str | None = None,
) -> int:
    """Persist the current state of the page (+ blocks) into ws_page_history.

    Trims history to most recent ``HISTORY_RETAIN`` snapshots per page.
    Returns the new history row id.
    """
    page = (await db.execute(text("""
        SELECT id, workspace_id, parent_id, title, icon, cover_url,
               slug, is_archived, position, created_by, updated_by,
               created_at, updated_at
          FROM ws_pages
         WHERE id = :id
    """), {"id": page_id})).first()
    if not page:
        raise ValueError(f"page {page_id} not found")

    blocks = (await db.execute(text("""
        SELECT id, parent_block_id, type, content, properties, position
          FROM ws_blocks
         WHERE page_id = :pid
         ORDER BY position ASC, id ASC
    """), {"pid": page_id})).all()

    snapshot = {
        "page": {
            "id": int(page[0]),
            "workspace_id": page[1],
            "parent_id": int(page[2]) if page[2] is not None else None,
            "title": page[3],
            "icon": page[4],
            "cover_url": page[5],
            "slug": page[6],
            "is_archived": bool(page[7]),
            "position": float(page[8]) if page[8] is not None else 0.0,
            "created_by": page[9],
            "updated_by": page[10],
            "created_at": page[11].isoformat() if page[11] else None,
            "updated_at": page[12].isoformat() if page[12] else None,
        },
        "blocks": [
            {
                "id": int(b[0]),
                "parent_block_id": int(b[1]) if b[1] is not None else None,
                "type": b[2],
                "content": b[3],
                "properties": _safe_jsonb(b[4]) or {},
                "position": float(b[5]) if b[5] is not None else 0.0,
            }
            for b in blocks
        ],
    }

    new_id = (await db.execute(text("""
        INSERT INTO ws_page_history (page_id, snapshot, edited_by, note)
        VALUES (:pid, CAST(:snap AS jsonb), :eb, :note)
        RETURNING id
    """), {
        "pid": page_id,
        "snap": json.dumps(snapshot),
        "eb": edited_by,
        "note": (note or "")[:200] if note else None,
    })).first()[0]

    # Trim old snapshots beyond HISTORY_RETAIN.
    try:
        await db.execute(text("""
            DELETE FROM ws_page_history
             WHERE page_id = :pid
               AND id NOT IN (
                   SELECT id FROM ws_page_history
                    WHERE page_id = :pid
                    ORDER BY created_at DESC
                    LIMIT :keep
               )
        """), {"pid": page_id, "keep": HISTORY_RETAIN})
    except Exception:
        log.exception("history trim failed page=%s", page_id)

    return int(new_id)


async def restore_snapshot(
    db: AsyncSession,
    *,
    page_id: int,
    version_id: int,
    edited_by: str | None,
) -> bool:
    """Restore a page (title/icon/cover) and its blocks from a snapshot.

    Strategy: take a fresh snapshot first (safety), then wipe blocks and
    rebuild from the chosen snapshot's data.
    """
    row = (await db.execute(text("""
        SELECT snapshot FROM ws_page_history
         WHERE id = :vid AND page_id = :pid
    """), {"vid": version_id, "pid": page_id})).first()
    if not row:
        return False
    snap = _safe_jsonb(row[0]) or {}
    if not isinstance(snap, dict):
        return False
    page_data = snap.get("page") or {}
    blocks_data = snap.get("blocks") or []

    # Safety snapshot (current state) before destructive change
    try:
        await take_snapshot(db, page_id=page_id, edited_by=edited_by,
                            note=f"pre-restore-of-{version_id}")
    except Exception:
        log.exception("safety snapshot failed page=%s", page_id)

    await db.execute(text("""
        UPDATE ws_pages SET
            title      = :title,
            icon       = :icon,
            cover_url  = :cover,
            updated_by = :eb
         WHERE id = :pid
    """), {
        "pid": page_id,
        "title": (page_data.get("title") or "Untitled")[:500],
        "icon": page_data.get("icon"),
        "cover": page_data.get("cover_url"),
        "eb": edited_by,
    })
    await db.execute(text("DELETE FROM ws_blocks WHERE page_id = :pid"),
                     {"pid": page_id})

    for idx, b in enumerate(blocks_data):
        if not isinstance(b, dict):
            continue
        await db.execute(text("""
            INSERT INTO ws_blocks
              (page_id, type, content, properties, position)
            VALUES
              (:pid, :type, :content, :props::jsonb, :pos)
        """), {
            "pid": page_id,
            "type": str(b.get("type") or "paragraph")[:20],
            "content": b.get("content"),
            "props": json.dumps(b.get("properties") or {}),
            "pos": float(b.get("position") or (idx + 1)),
        })
    return True
