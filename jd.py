"""Best-effort job description fetching. Returns "" when the page can't be read."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
MIN_LEN = 400


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _workday(u) -> str:
    # https://{tenant}.wdN.myworkdayjobs.com/[en-US/]{site}/(details|job)/.../{slug}
    parts = [p for p in u.path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    if len(parts) < 3:
        return ""
    tenant, site, slug = u.netloc.split(".")[0], parts[0], parts[-1]
    r = requests.get(f"https://{u.netloc}/wday/cxs/{tenant}/{site}/job/{slug}",
                     headers={**UA, "Accept": "application/json"}, timeout=20)
    r.raise_for_status()
    return _html_to_text(r.json()["jobPostingInfo"]["jobDescription"])


def _greenhouse(u) -> str:
    m = re.search(r"/([^/]+)/jobs/(\d+)", u.path)
    if not m:
        return ""
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}", timeout=20)
    r.raise_for_status()
    return _html_to_text(BeautifulSoup(r.json()["content"], "html.parser").get_text())


def _lever(u) -> str:
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        return ""
    r = requests.get(f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}", timeout=20)
    r.raise_for_status()
    d = r.json()
    lists = "\n\n".join(f"{x['text']}\n{_html_to_text(x['content'])}" for x in d.get("lists", []))
    return f"{d.get('descriptionPlain', '')}\n\n{lists}\n\n{d.get('additionalPlain', '')}".strip()


def fetch_jd(url: str) -> str:
    try:
        u = urlparse(url)
        if "myworkdayjobs.com" in u.netloc:
            text = _workday(u)
        elif "greenhouse.io" in u.netloc:
            text = _greenhouse(u)
        elif u.netloc == "jobs.lever.co":
            text = _lever(u)
        elif "linkedin.com" in u.netloc:
            return ""  # requires login
        else:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            text = _html_to_text(r.text)
        return text if len(text) >= MIN_LEN else ""
    except Exception as e:  # noqa: BLE001 - JD text is optional
        print(f"  ! JD fetch failed for {url}: {e}")
        return ""
