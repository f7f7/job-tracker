"""Use Claude to classify an application email and match it to an existing tracker row."""
from __future__ import annotations

import json
import os

import anthropic

from notion import APPLIED

# Must match the option names of the Notion Status property
STATUSES = ["Applied", "OA to do", "OA Done", "1st Interview", "Offer", "Rejected", "Withdrawn"]
MAX_BODY_CHARS = 60_000

SCHEMA = {
    "type": "object",
    "properties": {
        "is_job_related": {"type": "boolean"},
        "company": {"type": "string"},
        "job_title": {"type": "string"},
        "status": {"type": "string", "enum": STATUSES + ["No change"]},
        "job_id": {"type": "string"},
        "jd_url": {"type": "string"},
        "location": {"type": "string"},
        "deadline": {"type": "string"},
        "event": {"type": "string"},
        "match_page_id": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "review_reason": {"type": "string"},
    },
    "required": ["is_job_related", "company", "job_title", "status", "job_id", "jd_url", "location",
                 "deadline", "event", "match_page_id", "confidence", "review_reason"],
    "additionalProperties": False,
}

SYSTEM = f"""You process emails from a student's job-application mailbox and turn each into an update for their job tracker.

For the email, decide:
- is_job_related: false for newsletters, job alerts/recommendations, marketing, candidate-account/portal setup emails, or anything not about one of the user's own applications.
- company: the hiring company's common short name (e.g. "Booking" not "Booking.com B.V.", "Deutsche Bank"), not the ATS vendor (Workday, Greenhouse, SmartRecruiters...).
- job_title: the exact position title as written in the email, empty if not mentioned.
- status, one of:
  Applied = application received/confirmation;
  OA to do = invitation to an online assessment, coding test, HireVue/video interview or similar take-home task;
  OA Done = confirmation that an assessment was submitted/completed;
  1st Interview = invitation to schedule or attend any interview round;
  Rejected = the application will not move forward (also "position filled/closed");
  Offer = job offer;
  Withdrawn = the user withdrew;
  No change = job-related but none of the above (e.g. generic update, reminder of an existing OA).
- job_id: requisition / job ID (e.g. JR0288222, R-12345) if present.
- jd_url: a link to the public job posting itself if the email contains one; not login, portal, unsubscribe or tracking links.
- location: the city only, in English (e.g. "Munich" not "München", "Hamburg" not "Hamburg, Germany (Hybrid)");
  no country, region, street or work mode (Hybrid/Remote/On-site). Only if the email states it, else empty.
- deadline: due date of an OA/task or interview date as YYYY-MM-DD, else empty.
- event: only used when status is "No change": a 2-4 word English note for the timeline, e.g. "OA reminder", "Still in review"; else empty.
- match_page_id: the id of the tracker row this email is about, or empty if it is a new application not in the tracker.
  Match on company + title (titles in the tracker were typed by hand and may be abbreviated or differ slightly) and job_id.
  Different locations in the title (e.g. "- Berlin" vs "- Amsterdam") or different job IDs mean different applications: do not merge them.
  If the email names a position that clearly differs from every row of that company, it is a new application (empty match_page_id).
  A confirmation/update that names no position: match a row of that company whose applied date is close to the email date,
  rather than creating a new row (set confidence low if several rows fit).
  If the company has several rows and the email does not make clear which one, pick the most likely and set confidence low.
  Emails may be processed long after they arrived, so a row's status can already reflect later events
  (e.g. an old confirmation for a row now marked Rejected). That is expected and not a reason for low confidence.
- confidence: low whenever you are guessing the status, company, or the matched row.
- review_reason: one short sentence explaining what is uncertain, empty if confidence is high.
Emails may be in English, German or Chinese. Return empty strings for unknown fields."""


class Extractor:
    def __init__(self) -> None:
        self.client = anthropic.Anthropic()
        self.model = os.getenv("CLAUDE_MODEL") or "claude-opus-5-5"

    def extract(self, email: dict, rows: list[dict]) -> dict:
        body = email.get("body", {}).get("content", "")
        if len(body) > MAX_BODY_CHARS:
            print(f"  ! body truncated from {len(body)} to {MAX_BODY_CHARS} chars")
            body = body[:MAX_BODY_CHARS]
        tracker = [
            {"id": r["id"], "name": r.get("Name", ""), "company": r.get("Company", ""),
             "status": r.get("Status", ""), "job_id": r.get("Job ID", ""),
             "applied": r.get(APPLIED, ""), "timeline": r.get("Timeline", ""),
             "url": (r.get("URL") or "")[:150]}
            for r in rows
        ]
        sender = email.get("from", {}).get("emailAddress", {})
        user = (
            f"<tracker_rows>\n{json.dumps(tracker, ensure_ascii=False)}\n</tracker_rows>\n\n"
            f"<email>\nFrom: {sender.get('name', '')} <{sender.get('address', '')}>\n"
            f"Date: {email['receivedDateTime']}\nSubject: {email.get('subject', '')}\n\n{body}\n</email>"
        )
        kwargs = {}
        if self.model in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"):
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            **kwargs,
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Claude refused: {resp.stop_details}")
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)
