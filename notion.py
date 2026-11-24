"""Minimal Notion database client that reads/writes properties by their schema type."""
from __future__ import annotations

import os
import time

import requests

API = "https://api.notion.com/v1"
# Name of the "date applied" property in the Notion database
APPLIED = "Applied"

# Properties the tool adds if missing (Status options must be added by hand in Notion UI)
EXTRA_PROPERTIES = {
    "Deadline": {"date": {}},
    "Job ID": {"rich_text": {}},
    "Email link": {"url": {}},
    "Timeline": {"rich_text": {}},
    "Needs review": {"checkbox": {}},
    "Review note": {"rich_text": {}},
}


def _text_chunks(s: str) -> list[dict]:
    # Notion: max 2000 chars per text object, max 100 objects
    return [{"type": "text", "text": {"content": s[i:i + 2000]}} for i in range(0, min(len(s), 200_000), 2000)]


class Notion:
    def __init__(self) -> None:
        self.db = os.environ["NOTION_DATABASE_ID"].replace("-", "")
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
            "Notion-Version": "2022-06-28",
        })
        self.schema = self._req("GET", f"/databases/{self.db}")["properties"]

    def _req(self, method: str, path: str, body: dict | None = None, tries: int = 4) -> dict:
        # A create that timed out may still have succeeded on Notion's side: never retry it (avoids duplicates)
        creating = method == "POST" and path == "/pages"
        for attempt in range(tries):
            last = attempt == tries - 1
            try:
                r = self.s.request(method, API + path, json=body, timeout=60)
            except requests.exceptions.ConnectTimeout:
                if last:
                    raise
            except (requests.ConnectionError, requests.Timeout):
                if last or creating:
                    raise
            else:
                if r.ok:
                    return r.json()
                if r.status_code not in (429, 500, 502, 503, 504) or last:
                    raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text}")
            time.sleep(2 ** attempt * 2)
        raise AssertionError("unreachable")

    def ensure_schema(self, statuses: list[str], dry_run: bool = False) -> list[str]:
        """Add missing extra properties; return Status options that still need adding by hand."""
        missing = {k: v for k, v in EXTRA_PROPERTIES.items() if k not in self.schema}
        if missing and dry_run:
            print(f"(dry run) would add properties: {', '.join(missing)}")
        elif missing:
            self.schema = self._req("PATCH", f"/databases/{self.db}", {"properties": missing})["properties"]
        status = self.schema.get("Status", {})
        have = {o["name"] for o in status.get(status.get("type", ""), {}).get("options", [])}
        return [s for s in statuses if s not in have]

    # ---- reading ----
    @staticmethod
    def _read(prop: dict):
        t = prop["type"]
        v = prop.get(t)
        if t in ("title", "rich_text"):
            return "".join(x["plain_text"] for x in v)
        if t in ("select", "status"):
            return v["name"] if v else ""
        if t == "multi_select":
            return ", ".join(x["name"] for x in v)
        if t == "date":
            return v["start"] if v else ""
        if t in ("url", "checkbox", "email", "number"):
            return v
        return None

    def rows(self) -> list[dict]:
        out, cursor = [], None
        while True:
            body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            data = self._req("POST", f"/databases/{self.db}/query", body)
            for p in data["results"]:
                row = {k: self._read(v) for k, v in p["properties"].items()}
                row["id"] = p["id"]
                out.append(row)
            if not data["has_more"]:
                return out
            cursor = data["next_cursor"]

    # ---- writing ----
    def _value(self, name: str, value):
        t = self.schema[name]["type"]
        if t in ("title", "rich_text"):
            return {t: _text_chunks(str(value))}
        if t in ("select", "status"):
            return {t: {"name": value} if value else None}
        if t == "multi_select":
            return {t: [{"name": x.strip()} for x in str(value).split(",") if x.strip()]}
        if t == "date":
            return {t: {"start": value} if value else None}
        if t in ("url", "checkbox"):
            return {t: value or (False if t == "checkbox" else None)}
        raise ValueError(f"Unsupported property type {t} for {name}")

    def _props(self, values: dict) -> dict:
        unknown = [k for k in values if k not in self.schema]
        if unknown:
            print(f"  ! Notion 里没有这些属性，已跳过: {', '.join(unknown)}")
        return {k: self._value(k, v) for k, v in values.items() if k in self.schema}

    def create(self, values: dict) -> str:
        page = self._req("POST", "/pages", {"parent": {"database_id": self.db}, "properties": self._props(values)})
        return page["id"]

    def update(self, page_id: str, values: dict) -> None:
        self._req("PATCH", f"/pages/{page_id}", {"properties": self._props(values)})
