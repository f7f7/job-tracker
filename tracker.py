"""Turn unprocessed emails in the Outlook Application folder into Notion job-tracker rows.

Usage:
  python tracker.py --login          # one-time Outlook sign-in
  python tracker.py --dry-run        # print planned changes, write nothing
  python tracker.py --no-mark        # write Notion but leave emails unmarked (test database)
  python tracker.py [--limit N]      # process for real
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import outlook  # noqa: E402
from extract import STATUSES, Extractor  # noqa: E402
from jd import fetch_jd  # noqa: E402
from notion import APPLIED, Notion  # noqa: E402

TERMINAL = {"Rejected", "Offer", "HIRED", "Withdrawn"}


def local_date(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()


def short(d: str | datetime) -> str:
    if isinstance(d, str):
        d = datetime.fromisoformat(d[:10])
    return f"{d:%b} {d.day}"


def add_timeline(timeline: str, day: datetime, label: str) -> str:
    """Insert 'Mon D label' in date order; skip a repeated Applied or a repeat of the previous label."""
    entries = [e for e in timeline.split(" → ") if e.strip()]
    if label == "Applied" and any(e.endswith(" Applied") for e in entries):
        return timeline

    def key(e: str) -> datetime:
        try:
            return datetime.strptime(f"{day.year} {' '.join(e.split()[:2])}", "%Y %b %d")
        except ValueError:
            return datetime.min

    when = datetime(day.year, day.month, day.day)
    before = [e for e in entries if key(e) <= when]
    if before and before[-1].split(maxsplit=2)[-1] == label:
        return timeline
    entries.insert(len(before), f"{short(day)} {label}")
    return " → ".join(entries)


def flag(values: dict, existing_note: str, day: datetime, reason: str) -> None:
    """Set Needs review and append the reason to Review note."""
    values["Needs review"] = True
    line = f"{short(day)}: {reason}"
    note = values.get("Review note", existing_note or "")
    values["Review note"] = f"{note}\n{line}" if note else line


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def same_job(rows: list[dict], ext: dict) -> dict | None:
    """Existing row with the same company and title (or job ID); guards against duplicate rows."""
    if not ext["is_job_related"]:
        return None
    for r in rows:
        if _norm(r.get("Company")) != _norm(ext["company"]):
            continue
        if (ext["job_id"] and ext["job_id"] == r.get("Job ID")) or \
                (ext["job_title"] and _norm(ext["job_title"]) == _norm(r.get("Name"))):
            return r
    return None


def plan_update(row: dict, ext: dict, day: datetime, link: str) -> dict:
    status, current = ext["status"], row.get("Status") or ""
    iso = day.date().isoformat()
    u: dict = {"Email link": link}

    timeline = row.get("Timeline") or ""
    if not timeline and row.get(APPLIED):
        timeline = f"{short(row[APPLIED])} Applied"
    new_tl = add_timeline(timeline, day, status if status != "No change" else ext["event"] or status)
    if new_tl != (row.get("Timeline") or ""):
        u["Timeline"] = new_tl

    if status == "Applied":
        if not row.get(APPLIED):
            u[APPLIED] = iso
        if current in ("", "To Apply"):
            u["Status"] = "Applied"
    elif status != "No change":
        if current in TERMINAL and status not in TERMINAL:
            # e.g. OA invite after a rejection: leave it to the user
            flag(u, row.get("Review note"), day, f"Got a '{status}' email but status is already {current}")
        else:
            u["Status"] = status
        u["Next step @"] = iso

    if ext["deadline"]:
        u["Deadline"] = ext["deadline"]
    for prop, key in (("Job ID", "job_id"), ("URL", "jd_url"), ("Location", "location"),
                      ("Company", "company"), ("Name", "job_title")):
        if ext[key] and not row.get(prop):
            u[prop] = ext[key]
    if ext["job_title"] and (row.get("Name") or "").startswith(("http", "[?]")):
        u["Name"] = ext["job_title"]
    if ext["confidence"] != "high":
        flag(u, row.get("Review note"), day, ext["review_reason"] or "AI was not confident")
    return u


def plan_create(ext: dict, email: dict, day: datetime, link: str) -> dict:
    status = ext["status"] if ext["status"] != "No change" else "Applied"
    iso = day.date().isoformat()
    v = {
        "Name": ext["job_title"] or f"[?] {email.get('subject', '')}",
        "Company": ext["company"],
        "Status": status,
        "Timeline": f"{short(day)} {status}",
        "Email link": link,
        "Job ID": ext["job_id"],
        "URL": ext["jd_url"],
        "Location": ext["location"],
        "Deadline": ext["deadline"],
        "Needs review": False,
    }
    if ext["confidence"] != "high":
        flag(v, "", day, ext["review_reason"] or "AI was not confident")
    if status != "Applied":
        flag(v, "", day, f"First email seen is '{status}', applied date unknown")
    if status == "Applied":
        v[APPLIED] = iso
    else:
        v["Next step @"] = iso
    return {k: val for k, val in v.items() if val not in ("", None)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-mark", action="store_true", help="don't tag emails as processed")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.login:
        outlook.login()
        return 0

    print(f"=== run {datetime.now():%Y-%m-%d %H:%M} {'(dry run)' if args.dry_run else ''}")
    wait_for_network()
    notion = Notion()
    missing = notion.ensure_schema(STATUSES, dry_run=args.dry_run)
    if missing:
        print(f"! Notion Status 缺少选项 {', '.join(missing)}，遇到时只记 Timeline 并标记 Needs review")
    rows = notion.rows()
    mail = outlook.Outlook()
    msgs = mail.unprocessed_messages(mail.find_folder(os.getenv("OUTLOOK_FOLDER", "Application")))
    if args.limit:
        msgs = msgs[: args.limit]
    print(f"{len(rows)} tracker rows, {len(msgs)} unprocessed emails")

    extractor = Extractor()
    by_id = {r["id"]: r for r in rows}
    errors = 0
    for i, m in enumerate(msgs, 1):
        day = local_date(m["receivedDateTime"])
        print(f"\n[{i}/{len(msgs)}] {day:%Y-%m-%d} | {m.get('subject', '')[:90]}")
        try:
            ext = extractor.extract(m, rows)
            print(f"  → {ext['company']} | {ext['job_title']} | {ext['status']} | {ext['confidence']}"
                  + (f" ({ext['review_reason']})" if ext["review_reason"] else ""))
            if ext["status"] in missing:
                ext["event"] = ext["event"] or ext["status"]
                ext["review_reason"] = f"Status option '{ext['status']}' is missing in Notion"
                ext["status"], ext["confidence"] = "No change", "low"
            row = by_id.get(ext["match_page_id"]) or same_job(rows, ext)
            if not ext["is_job_related"]:
                print("  skip: not about an application")
            elif not row and ext["status"] == "No change":
                print("  skip: no status change and no matching row")
            else:
                if row:
                    values = plan_update(row, ext, day, m.get("webLink", ""))
                    print(f"  UPDATE '{row.get('Name')}' @ {row.get('Company')}: {values}")
                else:
                    values = plan_create(ext, m, day, m.get("webLink", ""))
                    print(f"  CREATE {values}")
                    row = {"id": f"new-{i}", "Name": values["Name"], "Company": values.get("Company", "")}
                    rows.append(row)
                    by_id[row["id"]] = row

                url = values.get("URL") or row.get("URL")
                if url and not row.get("Text"):
                    text = fetch_jd(url)
                    if text:
                        values["Text"] = text
                        print(f"  + JD text ({len(text)} chars)")

                if not args.dry_run:
                    if row["id"].startswith("new-"):
                        new_id = notion.create(values)
                        by_id[new_id] = by_id.pop(row["id"])
                        row["id"] = new_id
                    else:
                        notion.update(row["id"], values)
                row.update(values)

            if not (args.dry_run or args.no_mark):
                mail.mark_processed(m)
        except Exception as e:  # noqa: BLE001 - keep going; the email stays unprocessed for the next run
            errors += 1
            print(f"  ERROR: {e}")

    print(f"\ndone, {errors} errors")
    if errors:
        notify(f"{errors} 封邮件处理失败，详见 logs/tracker.log")
    return 1 if errors else 0


def wait_for_network(host: str = "api.notion.com", timeout: int = 600) -> None:
    """launchd fires right after wake, before Wi-Fi/DNS is back; poll until the host resolves."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.getaddrinfo(host, 443)
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(15)


def notify(msg: str) -> None:
    if sys.platform != "darwin":  # GitHub Actions: a failed run already triggers GitHub's email
        return
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Job tracker"'], check=False)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # setup failures, e.g. expired Outlook token
        notify(f"运行失败: {str(e)[:120]}".replace('"', "'"))
        raise
