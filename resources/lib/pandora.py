"""
Minimal client for Pandora's (unofficial) REST API — the same JSON API the
pandora.com web player uses. No API keys are required; you authenticate with
your own account credentials.

NOTE: This API is unofficial and undocumented. Pandora can change or break it
at any time, and it only works from US IP addresses.
"""

from __future__ import annotations

import json
import http.cookiejar
import time
import urllib.request
import urllib.error
from typing import Any

BASE = "https://www.pandora.com"
API = BASE + "/api"

# Wire-level tracing of every HTTP exchange with Pandora. Deliberately a
# code-level toggle, not a setting: it is verbose and should stay off even
# when Kodi debug logging is active. Flip to True while diagnosing, then
# flip back. Output goes to Kodi's debug log (or stdout outside Kodi).
NETWORK_TRACE: bool = True


def _net_trace(msg: str) -> None:
    if not NETWORK_TRACE:
        return
    line = "plugin.audio.pandora [net] %s" % msg
    try:
        import xbmc
        xbmc.log(line, xbmc.LOGDEBUG)
    except ImportError:
        print(line)  # running outside Kodi (desktop test script)


class PandoraError(Exception):
    pass


class PandoraClient:
    def __init__(self) -> None:
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.csrf_token: str | None = None
        self.auth_token: str | None = None

    # ---------- internals ----------

    def _get_csrf(self) -> None:
        """Pandora sets a csrftoken cookie on any page load; the API requires
        it echoed back in the X-CsrfToken header."""
        _net_trace("GET %s/ (csrf bootstrap)" % BASE)
        req = urllib.request.Request(BASE + "/", headers={"User-Agent": "Mozilla/5.0"})
        started = time.monotonic()
        try:
            self.opener.open(req, timeout=15).read()
        except urllib.error.HTTPError as e:
            _net_trace("GET / -> HTTP %s (ignored; error pages still set cookie)" % e.code)
        else:
            _net_trace("GET / -> 200 in %dms" % ((time.monotonic() - started) * 1000))
        for c in self.cookies:
            if c.name == "csrftoken":
                self.csrf_token = c.value
                _net_trace("csrf cookie acquired (len=%d)" % len(c.value))
                return
        raise PandoraError("Could not obtain CSRF token (is Pandora reachable from your region?)")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "X-CsrfToken": self.csrf_token or "",
        }
        if self.auth_token:
            headers["X-AuthToken"] = self.auth_token
        # Log key names only — values include the password and track tokens.
        _net_trace("POST %s keys=%s auth_header=%s"
                   % (path, payload.values(), "yes" if self.auth_token else "no"))
        req = urllib.request.Request(
            API + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        try:
            resp = self.opener.open(req, timeout=20)
            raw = resp.read()
            _net_trace("POST %s -> %s, %d bytes in %dms"
                       % (path, resp.status, len(raw),
                          (time.monotonic() - started) * 1000))
        except urllib.error.HTTPError as e:
            raw = e.read()
            snippet = raw[:300].decode("utf-8", errors="replace")
            _net_trace("POST %s -> HTTP %s in %dms, body[:300]=%r"
                       % (path, e.code, (time.monotonic() - started) * 1000, snippet))
            try:
                body = json.loads(raw.decode("utf-8"))
                msg = body.get("message") or body.get("errorString") or str(e)
            except Exception:
                msg = str(e)
            raise PandoraError("%s (%s)" % (msg, path))

        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            # 200-with-non-JSON is Pandora's classic "session no longer valid"
            # symptom (an HTML page instead of API JSON). Surface it as a
            # PandoraError so with_client's clear-and-retry logic can fire.
            snippet = raw[:300].decode("utf-8", errors="replace")
            _net_trace("POST %s -> %s but body is not JSON, body[:300]=%r"
                       % (path, resp.status, snippet))
            raise PandoraError("Non-JSON response from %s (session expired?)" % path)

    # ---------- public API ----------

    def login(self, username: str, password: str) -> dict[str, Any]:
        if not self.csrf_token:
            self._get_csrf()
        data = self._post("/v1/auth/login", {
            "username": username,
            "password": password,
            "keepLoggedIn": True,
        })
        self.auth_token = data.get("authToken")
        if not self.auth_token:
            raise PandoraError("Login succeeded but no auth token returned")
        return data

    def get_stations(self, page_size: int = 250) -> list[dict[str, Any]]:
        data = self._post("/v1/station/getStations", {"pageSize": page_size})
        return data.get("stations", [])

    def get_fragment(self, station_id: str, is_start: bool = False) -> list[dict[str, Any]]:
        """Returns the next handful of tracks for a station."""
        data = self._post("/v1/playlist/getFragment", {
            "stationId": station_id,
            "isStationStart": is_start,
            "fragmentRequestReason": "Normal",
            "audioFormat": "aacplus",
            "startingAtTrackId": None,
            "onDemandArtistMessageArtistUidHex": None,
            "onDemandArtistMessageIdHex": None,
        })
        return data.get("tracks", [])

    def feedback(self, track_token: str, is_positive: bool) -> dict[str, Any]:
        """Thumbs up / down for a track."""
        return self._post("/v1/station/addFeedback", {
            "trackToken": track_token,
            "isPositive": bool(is_positive),
        })

    def search(self, query: str, count: int = 20) -> list[dict[str, Any]]:
        """Search artists/tracks that can seed a new station.

        Returns a list of dicts with pandoraId, type ('AR'/'TR'/...), and
        display fields. Uses the v3 search endpoint the web player uses.
        """
        data = self._post("/v3/sod/search", {
            "query": query,
            "types": ["ar", "tr"],
            "listener": None,
            "start": 0,
            "count": count,
            "annotate": True,
            "searchTime": 0,
        })
        annotations = data.get("annotations", {})
        results: list[dict[str, Any]] = []
        for pid in data.get("results", []):
            ann = annotations.get(pid)
            if ann:
                results.append(ann)
        return results

    def create_station(self, pandora_id: str, query: str = "") -> dict[str, Any]:
        """Create a station seeded from a search result's pandoraId."""
        return self._post("/v1/station/createStation", {
            "pandoraId": pandora_id,
            "stationCode": None,
            "searchQuery": query,
            "creativeSource": "search",
        })

    def delete_station(self, station_id: str) -> dict[str, Any]:
        return self._post("/v1/station/removeStation", {
            "stationId": station_id,
        })
