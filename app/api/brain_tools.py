"""Brain tools: the wiki and the experiments database."""

from app.services import brain_db, wiki_service


class BrainTools:
    def __init__(self, proposed_by: str = "professor agent"):
        self.proposed_by = proposed_by

    def wiki_index(self) -> dict:
        return {
            "pages": [
                {"page": p["page"], "title": p["title"], "type": p["type"], "summary": p["summary"]}
                for p in wiki_service.list_pages()
            ],
            "next": "wiki_read a page by its name; wiki_search to find pages about a topic.",
        }

    def wiki_search(self, query: str, limit: int = 8) -> dict:
        results = wiki_service.search(query, max(1, min(int(limit), 20)))
        if not results:
            return {"results": [], "note": "Nothing matches; try other words or wiki_index."}
        return {"results": results}

    def wiki_read(self, page: str) -> dict:
        found = wiki_service.read_page(page)
        return {
            "page": found["page"],
            "title": found["meta"].get("title"),
            "type": found["meta"].get("type"),
            "sources": found["meta"].get("sources"),
            "updated": found["meta"].get("updated"),
            "content": found["content"],
            "links": found["links"],
            "linked_from": found["backlinks"],
        }

    def wiki_propose(self, page: str, content: str, reason: str) -> dict:
        proposal = wiki_service.propose(page, content, reason, self.proposed_by)
        return {
            "proposal": proposal["id"],
            "page": proposal["page"],
            "status": proposal["status"],
            "new_page": proposal["base_version"] is None,
            "next": "Nothing is changed yet: a person reviews the proposal in Deepvac Insight (Wiki Proposals).",
        }

    def sql_schema(self) -> dict:
        return brain_db.schema()

    def sql_query(self, sql: str, max_rows: int = 50) -> dict:
        return brain_db.query(sql, max_rows)
