"""The LLM wiki: the crew's knowledge of the chamber, as markdown pages ("the Brain", with the
experiments database in brain_db.py).

    wiki/
      SCHEMA.md     how pages are written (read it before proposing an edit)
      index.md      every page with its summary, grouped by type (rebuilt, never edited)
      log.md        what changed and why, newest last (appended)
      <page>.md     one topic per page, named in kebab-case

A page starts with YAML-style frontmatter (title, type, tags, sources, updated) and links to
other pages as [[page-name]]. Types: concept, procedure, reference, lesson, generated.
"generated" pages are rewritten from the data by scripts/build_brain.py and can't be edited.

Agents read the wiki (search, read, index) and may only propose changes: a proposal waits in
data/deepvac_wiki_proposals.sqlite3 until a person approves it in Insight (Wiki Proposals), as
chamber requests do. An approval writes the page, rebuilds the index and adds a log entry; a
proposal made against an older version of the page is refused rather than merged.

The wiki lives in the source tree (insight/wiki, versioned with git); a frozen build keeps a
writable copy in DATA_DIR/wiki, seeded from the bundle. DEEPVAC_WIKI_DIR overrides both.
"""

import hashlib
import math
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import app.common as common

PAGE_TYPES = ("concept", "procedure", "reference", "lesson", "generated")
RESERVED = ("index", "log", "SCHEMA")
PAGE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
TOKEN = re.compile(r"[a-z0-9°]+(?:[.,][0-9]+)?")

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


class WikiError(ValueError):
    pass


# ---------------------------------------------------------------- where


def wiki_dir() -> Path:
    if os.environ.get("DEEPVAC_WIKI_DIR"):
        return Path(os.environ["DEEPVAC_WIKI_DIR"])
    source = Path(__file__).resolve().parents[2] / "wiki"
    if not getattr(sys, "frozen", False):
        return source
    live = common.DATA_DIR / "wiki"
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "wiki"
    if bundled.is_dir():  # seed: pages the live copy doesn't have yet
        live.mkdir(parents=True, exist_ok=True)
        for page in bundled.glob("*.md"):
            if not (live / page.name).exists():
                shutil.copy2(page, live / page.name)
    return live


def proposals_db() -> Path:
    return common.DATA_DIR / "deepvac_wiki_proposals.sqlite3"


# ---------------------------------------------------------------- pages


def parse(text: str) -> tuple[dict, str]:
    """(frontmatter, body). Values: strings, or lists for [a, b] and "- item" lines."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta, key = {}, None
    for line in text[3:end].strip().splitlines():
        if re.match(r"^\s+-\s+", line) and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(line.split("-", 1)[1].strip())
            continue
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key] = value.strip("'\"") if value else []
    return meta, text[end + 4 :].lstrip("\n")


def _without_code(text: str) -> str:
    """Text outside code blocks and inline code (examples there are not links)."""
    return re.sub(r"`[^`\n]*`", "", re.sub(r"```.*?```", "", text, flags=re.S))


def _summary(body: str) -> str:
    """The first paragraph that isn't a heading or a note."""
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if block and not block.startswith(("#", ">", "|", "```", "<!--")):
            text = re.sub(r"\s+", " ", block)
            return text if len(text) <= 220 else text[:217].rstrip() + "…"
    return ""


def page_path(page_id: str) -> Path:
    return wiki_dir() / f"{page_id}.md"


def list_pages() -> list[dict]:
    pages = []
    for path in sorted(wiki_dir().glob("*.md")):
        if path.stem in RESERVED:
            continue
        meta, body = parse(path.read_text(encoding="utf-8"))
        pages.append(
            {
                "page": path.stem,
                "title": meta.get("title") or path.stem,
                "type": meta.get("type") or "",
                "tags": meta.get("tags") or [],
                "updated": meta.get("updated") or "",
                "summary": _summary(body),
            }
        )
    return pages


def read_page(page_id: str) -> dict:
    page_id = page_id.strip().removesuffix(".md")
    path = page_path(page_id)
    if not path.exists():
        close = [p["page"] for p in search(page_id.replace("-", " "), 3)]
        raise WikiError(
            f"No page '{page_id}'." + (f" Closest: {', '.join(close)}." if close else "")
        )
    text = path.read_text(encoding="utf-8")
    meta, body = parse(text)
    links = sorted(set(LINK.findall(_without_code(body))))
    backlinks = sorted(
        p.stem
        for p in wiki_dir().glob("*.md")
        if p.stem not in RESERVED
        and p.stem != page_id
        and f"[[{page_id}" in p.read_text(encoding="utf-8")
    )
    return {
        "page": page_id,
        "meta": meta,
        "content": body,
        "links": links,
        "backlinks": backlinks,
        "version": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
    }


def _stem(word: str) -> str:
    """A light stemmer: plurals and common endings, so "definitions" finds "definition"."""
    for ending in ("ings", "ing", "ies", "es", "s", "ed"):
        if len(word) > len(ending) + 3 and word.endswith(ending):
            return word[: -len(ending)] + ("y" if ending == "ies" else "")
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in TOKEN.findall(text.lower())]


def search(query: str, limit: int = 8) -> list[dict]:
    """BM25 over the pages; the title and tags count more than the body."""
    terms = _tokens(query)
    if not terms:
        return []
    docs = []
    for path in sorted(wiki_dir().glob("*.md")):
        if path.stem in RESERVED:
            continue
        meta, body = parse(path.read_text(encoding="utf-8"))
        title = str(meta.get("title") or path.stem)
        tags = " ".join(meta.get("tags") or [])
        tokens = (
            _tokens(title) * 3
            + _tokens(tags + " " + path.stem.replace("-", " ")) * 2
            + _tokens(body)
        )
        docs.append((path.stem, title, body, Counter(tokens), len(tokens)))
    if not docs:
        return []
    average = sum(d[4] for d in docs) / len(docs)
    k1, b = 1.2, 0.9  # long pages (tables of numbers) don't win by repetition
    scored = []
    for page, title, body, counts, length in docs:
        score = 0.0
        for term in set(terms):
            df = sum(1 for d in docs if term in d[3])
            if not df or term not in counts:
                continue
            idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
            tf = counts[term]
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / average))
        if score > 0:
            scored.append((score, page, title, body))
    scored.sort(reverse=True)
    results = []
    for score, page, title, body in scored[:limit]:
        results.append(
            {
                "page": page,
                "title": title,
                "score": round(score, 2),
                "snippet": _snippet(body, terms),
            }
        )
    return results


def _snippet(body: str, terms: list[str], width: int = 220) -> str:
    flat = re.sub(r"\s+", " ", body)
    lower = flat.lower()
    hits = [lower.find(t) for t in terms if lower.find(t) >= 0]
    start = max(0, min(hits) - 60) if hits else 0
    text = flat[start : start + width].strip()
    return ("…" if start else "") + text + ("…" if start + width < len(flat) else "")


def rebuild_index() -> Path:
    """index.md: every page by type, with its summary (what an agent reads first)."""
    pages = list_pages()
    order = {
        t: i for i, t in enumerate(("concept", "procedure", "reference", "lesson", "generated"))
    }
    lines = [
        "# DeepVac wiki: index",
        "",
        "Every page with its summary. Rebuilt automatically: don't edit. How pages are written: "
        "[[SCHEMA]]; what changed: [[log]].",
    ]
    names = {
        "concept": "Concepts",
        "procedure": "Procedures",
        "reference": "Reference",
        "lesson": "Lessons",
        "generated": "Generated from the data",
    }
    for kind in sorted({p["type"] for p in pages}, key=lambda t: order.get(t, 99)):
        lines += ["", f"## {names.get(kind, kind or 'Other')}", ""]
        for p in (p for p in pages if p["type"] == kind):
            lines.append(f"- [[{p['page']}]] **{p['title']}**: {p['summary']}")
    path = wiki_dir() / "index.md"
    _write(path, "\n".join(lines) + "\n")
    return path


def append_log(what: str, who: str = "", reason: str = "") -> None:
    path = wiki_dir() / "log.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (
        f"- {stamp}"
        + (f" · {who}" if who else "")
        + f": {what}"
        + (f" ({reason})" if reason else "")
    )
    text = (
        path.read_text(encoding="utf-8")
        if path.exists()
        else "# DeepVac wiki: log\n\nNewest last.\n\n"
    )
    _write(path, text.rstrip("\n") + "\n" + entry + "\n")


def lint() -> list[str]:
    """Problems: missing frontmatter or type, links to pages that don't exist, pages nothing links to."""
    problems = []
    ids = {p.stem for p in wiki_dir().glob("*.md")}
    linked = set()
    for path in sorted(wiki_dir().glob("*.md")):
        meta, body = parse(path.read_text(encoding="utf-8"))
        for link in LINK.findall(_without_code(body)):
            linked.add(link)
            if link not in ids:
                problems.append(f"{path.stem}: link to missing page [[{link}]]")
        if path.stem in RESERVED:
            continue
        if not meta:
            problems.append(f"{path.stem}: no frontmatter")
        elif meta.get("type") not in PAGE_TYPES:
            problems.append(
                f"{path.stem}: type '{meta.get('type')}' is not one of {', '.join(PAGE_TYPES)}"
            )
        if not meta.get("sources"):
            problems.append(f"{path.stem}: no sources")
    for page in sorted(ids - linked - set(RESERVED)):
        problems.append(f"{page}: no page links to it")
    return problems


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_generated(page_id: str, text: str) -> None:
    """A page rewritten from the data (scripts/build_brain.py)."""
    _write(page_path(page_id), text)


# ---------------------------------------------------------------- proposals: agents write, people approve


def _connect():
    path = proposals_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            page TEXT NOT NULL,
            content TEXT NOT NULL,
            reason TEXT NOT NULL,
            proposed_by TEXT NOT NULL,
            base_version TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_at TEXT,
            decided_by TEXT,
            comment TEXT
        )
        """
    )
    conn.commit()
    return conn


def _row(r) -> dict:
    return dict(r)


def propose(page_id: str, content: str, reason: str, proposed_by: str = "agent") -> dict:
    """A new page or a new version of one, waiting for a person's approval."""
    page_id = page_id.strip().removesuffix(".md")
    if not PAGE_ID.match(page_id) or page_id in RESERVED:
        raise WikiError(
            "The page name must be kebab-case (e.g. 'r23-compressor-start-delay') and not index/log/SCHEMA."
        )
    if not reason or not reason.strip():
        raise WikiError("Give the reason: the reviewer reads it when deciding.")
    meta, body = parse(content)
    if (
        not meta.get("title")
        or meta.get("type") not in PAGE_TYPES
        or meta.get("type") == "generated"
    ):
        raise WikiError(
            "Start the page with frontmatter: title, type (concept, procedure, reference or lesson), tags, "
            "sources and updated, as SCHEMA describes."
        )
    if not meta.get("sources"):
        raise WikiError(
            "Say where the content comes from (sources: run numbers, documents, people)."
        )
    current = page_path(page_id)
    base = None
    if current.exists():
        current_meta, _ = parse(current.read_text(encoding="utf-8"))
        if current_meta.get("type") == "generated":
            raise WikiError(f"'{page_id}' is generated from the data and can't be edited.")
        base = read_page(page_id)["version"]
    if not body.strip():
        raise WikiError("The page has no content.")
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO proposals (created_at, page, content, reason, proposed_by, base_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                page_id,
                content,
                reason.strip(),
                proposed_by,
                base,
            ),
        )
        conn.commit()
        return _row(
            conn.execute("SELECT * FROM proposals WHERE id = ?", (cur.lastrowid,)).fetchone()
        )
    finally:
        conn.close()


def list_proposals(status: str | None = PENDING, limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def get_proposal(proposal_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM proposals WHERE id = ?", (int(proposal_id),)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def decide(proposal_id: int, approve: bool, user_name: str, comment: str = "") -> dict:
    """A person's decision. Approving writes the page; a page changed since the proposal refuses it."""
    if not user_name:
        raise WikiError("Only a signed-in user can decide on a proposal.")
    proposal = get_proposal(proposal_id)
    if proposal is None or proposal["status"] != PENDING:
        raise WikiError(f"Proposal {proposal_id} is not pending.")
    if approve:
        exists = page_path(proposal["page"]).exists()
        current = read_page(proposal["page"])["version"] if exists else None
        if current != proposal["base_version"]:
            raise WikiError(
                f"'{proposal['page']}' changed after this proposal was made; reject it and ask for a new one."
            )
        _write(page_path(proposal["page"]), proposal["content"].rstrip("\n") + "\n")
        rebuild_index()
        append_log(
            f"{'updated' if exists else 'created'} [[{proposal['page']}]] (proposal {proposal['id']} by "
            f"{proposal['proposed_by']}, approved)",
            user_name,
            proposal["reason"],
        )
    conn = _connect()
    try:
        conn.execute(
            "UPDATE proposals SET status = ?, decided_at = ?, decided_by = ?, comment = ? WHERE id = ?",
            (
                APPROVED if approve else REJECTED,
                datetime.now(timezone.utc).isoformat(),
                user_name,
                comment,
                proposal_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_proposal(proposal_id)
