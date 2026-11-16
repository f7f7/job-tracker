"""Read the Application folder from Outlook via Microsoft Graph."""
from __future__ import annotations

import os
from pathlib import Path

import msal
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite"]
CACHE_FILE = Path(__file__).parent / "token_cache.json"
PROCESSED_CATEGORY = "JobTracker"


def _app() -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    cache = msal.SerializableTokenCache()
    if CACHE_FILE.exists():
        cache.deserialize(CACHE_FILE.read_text())
    authority = f"https://login.microsoftonline.com/{os.getenv('MS_AUTHORITY', 'consumers')}"
    app = msal.PublicClientApplication(os.environ["MS_CLIENT_ID"], authority=authority, token_cache=cache)
    return app, cache


def _save(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        CACHE_FILE.write_text(cache.serialize())
        CACHE_FILE.chmod(0o600)


def login() -> None:
    """Interactive device-code login; run once, the refresh token is cached afterwards."""
    app, cache = _app()
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow}")
    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"Login failed: {result.get('error_description')}")
    _save(cache)
    print("Outlook 登录成功，token 已缓存。")


def _token(force_refresh: bool = False) -> str:
    app, cache = _app()
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0], force_refresh=force_refresh) if accounts else None
    if not result or "access_token" not in result:
        raise RuntimeError("Outlook token 不可用，请先运行: python tracker.py --login")
    _save(cache)
    return result["access_token"]


class Outlook:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers["Prefer"] = 'outlook.body-content-type="text"'
        self._auth()

    def _auth(self, force_refresh: bool = False) -> None:
        self.s.headers["Authorization"] = f"Bearer {_token(force_refresh)}"

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        """Access tokens expire after ~1h; on 401 fetch a fresh one and retry once."""
        r = self.s.request(method, url, timeout=30, **kw)
        if r.status_code == 401:
            self._auth(force_refresh=True)
            r = self.s.request(method, url, timeout=30, **kw)
        r.raise_for_status()
        return r

    def _get(self, url: str, **params) -> dict:
        return self._request("GET", url, params=params or None).json()

    def find_folder(self, name: str) -> str:
        """Search Inbox children first, then top-level folders."""
        for url in (f"{GRAPH}/me/mailFolders/inbox/childFolders", f"{GRAPH}/me/mailFolders"):
            for f in self._get(url, **{"$top": 200}).get("value", []):
                if f["displayName"].strip().lower() == name.lower():
                    return f["id"]
        raise RuntimeError(f"找不到邮件文件夹: {name}")

    def unprocessed_messages(self, folder_id: str) -> list[dict]:
        """All messages without the processed category, oldest first."""
        url = f"{GRAPH}/me/mailFolders/{folder_id}/messages"
        params = {
            "$select": "id,subject,from,receivedDateTime,body,webLink,categories",
            "$orderby": "receivedDateTime asc",
            "$top": 50,
        }
        out = []
        while url:
            data = self._get(url, **params)
            out += [m for m in data["value"] if PROCESSED_CATEGORY not in m.get("categories", [])]
            url, params = data.get("@odata.nextLink"), {}
        return out

    def mark_processed(self, msg: dict) -> None:
        cats = msg.get("categories", []) + [PROCESSED_CATEGORY]
        self._request("PATCH", f"{GRAPH}/me/messages/{msg['id']}", json={"categories": cats})
