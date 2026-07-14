from __future__ import annotations

import http.cookiejar
import sys
import urllib.parse
from typing import Any, Callable, TypeVar

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

sys.path.insert(0, xbmcaddon.Addon().getAddonInfo("path") + "/resources/lib")
from pandora import PandoraClient, PandoraError  # noqa: E402

ADDON = xbmcaddon.Addon()
HANDLE: int = int(sys.argv[1])
BASE_URL: str = sys.argv[0]

# One client per plugin invocation. The auth token is cached in a window
# property so we don't log in on every directory listing / track fetch.
_PROP_TOKEN = "plugin.audio.pandora.authtoken"
_PROP_CSRF = "plugin.audio.pandora.csrf"

T = TypeVar("T")


def build_url(**kwargs: str) -> str:
    return BASE_URL + "?" + urllib.parse.urlencode(kwargs)


def notify(message: str, error: bool = False) -> None:
    icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
    xbmcgui.Dialog().notification("Pandora", message, icon, 4000)


def trace(msg: str) -> None:
    """Session-lifecycle tracing. Visible only with Kodi debug logging on."""
    xbmc.log("plugin.audio.pandora [trace] %s" % msg, xbmc.LOGDEBUG)


def _tok(value: str | None) -> str:
    """Loggable fingerprint of a token: length + first 6 chars, never the whole
    thing, so debug logs can be shared without leaking a usable session."""
    if not value:
        return "<empty>"
    return "len=%d %s..." % (len(value), value[:6])


# --- session primitives ----------------------------------------------------
# These never touch add-on settings or the UI. Credential lookup and user
# interaction stay in the callers (get_client, do_test_login).

def make_client() -> PandoraClient:
    """Construct a client and restore any cached session. No network I/O."""
    client = PandoraClient()
    win = xbmcgui.Window(10000)
    token = win.getProperty(_PROP_TOKEN)
    csrf = win.getProperty(_PROP_CSRF)
    if token and csrf:
        trace("make_client: cache HIT auth=%s csrf=%s" % (_tok(token), _tok(csrf)))
        client.auth_token = token
        client.csrf_token = csrf
        client.cookies.set_cookie(_csrf_cookie(csrf))
    else:
        trace("make_client: cache MISS (auth %s, csrf %s)"
              % ("set" if token else "empty", "set" if csrf else "empty"))
    return client


def login(client: PandoraClient, username: str, password: str) -> PandoraClient:
    """Authenticate (CSRF dance + login call) and cache the session."""
    trace("login: starting fresh login for %r" % username)
    client.login(username, password)
    trace("login: success, caching auth=%s csrf=%s"
          % (_tok(client.auth_token), _tok(client.csrf_token)))
    win = xbmcgui.Window(10000)
    win.setProperty(_PROP_TOKEN, client.auth_token)
    win.setProperty(_PROP_CSRF, client.csrf_token)
    return client


def _csrf_cookie(csrf: str) -> http.cookiejar.Cookie:
    # Rebuild the csrftoken cookie the X-CsrfToken header must match.
    return http.cookiejar.Cookie(
        0, "csrftoken", csrf, None, False, "www.pandora.com", True, False,
        "/", True, True, None, False, None, None, {})


# --- session policy ---------------------------------------------------------

def get_client() -> PandoraClient:
    """Ensure-session: cached client if one exists, otherwise log in with
    configured credentials. Prompts for settings if none are configured."""
    client = make_client()
    if client.auth_token:
        return client

    username = ADDON.getSetting("username").strip()
    password = ADDON.getSetting("password")
    if not username or not password:
        notify("Set your Pandora email and password in the add-on settings", error=True)
        ADDON.openSettings()
        raise PandoraError("No credentials configured")

    return login(client, username, password)


def clear_session() -> None:
    trace("clear_session: dropping cached tokens")
    win = xbmcgui.Window(10000)
    win.clearProperty(_PROP_TOKEN)
    win.clearProperty(_PROP_CSRF)


def with_client(fn: Callable[[PandoraClient], T]) -> tuple[PandoraClient, T]:
    """Run fn(client); on auth failure, clear the cached session and retry once."""
    client = get_client()
    try:
        return client, fn(client)
    except PandoraError as e:
        trace("with_client: call failed (%s) -> clearing session, retrying once" % e)
        clear_session()
        client = get_client()
        result = client, fn(client)
        trace("with_client: retry succeeded after fresh login")
        return result


def list_stations() -> None:
    xbmcplugin.setPluginCategory(HANDLE, "Stations")
    xbmcplugin.setContent(HANDLE, "songs")
    try:
        _, stations = with_client(lambda c: c.get_stations())
    except PandoraError as e:
        notify(str(e), error=True)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    search_li = xbmcgui.ListItem(label="[B]Search / Create station…[/B]")
    xbmcplugin.addDirectoryItem(HANDLE, build_url(action="search"), search_li, isFolder=True)

    for st in stations:
        name = st.get("name", "Unknown station")
        li = xbmcgui.ListItem(label=name)
        art = st.get("art") or []
        if art:
            url = art[-1].get("url")
            if url:
                li.setArt({"thumb": url, "icon": url})
        li.setInfo("music", {"title": name})
        li.addContextMenuItems([(
            "Delete station",
            "RunPlugin(%s)" % build_url(action="delete_station",
                                        station_id=st["stationId"],
                                        name=name),
        )])
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="station", station_id=st["stationId"], start="1"),
            li,
            isFolder=True,
        )
    xbmcplugin.endOfDirectory(HANDLE)


def track_listitem(track: dict[str, Any]) -> xbmcgui.ListItem:
    title = track.get("songTitle", "Unknown")
    artist = track.get("artistName", "")
    album = track.get("albumTitle", "")
    li = xbmcgui.ListItem(label="%s - %s" % (artist, title))

    tag = li.getMusicInfoTag()
    tag.setTitle(title)
    if artist:
        tag.setArtists([artist])
    if album:
        tag.setAlbum(album)
    tag.setDuration(int(track.get("trackLength", 0)))
    if int(track.get("rating", 0)) > 0:
        tag.setUserRating(10)          # Pandora thumbs-up -> max user rating

    art_url = track.get("albumArt", [])
    ...  # rest unchanged
    art_url = track.get("albumArt", [])
    if art_url:
        url = art_url[-1].get("url")
        if url:
            li.setArt({"thumb": url, "icon": url})
    li.setProperty("IsPlayable", "true")
    token = track.get("trackToken")
    if token:
        li.addContextMenuItems([
            ("Thumbs up",
             "RunPlugin(%s)" % build_url(action="feedback", token=token, positive="1")),
            ("Thumbs down",
             "RunPlugin(%s)" % build_url(action="feedback", token=token, positive="0")),
        ])
    return li


def list_station_tracks(station_id: str, is_start: bool) -> None:
    """Fetch a playlist fragment and list its tracks as playable items,
    with a 'More…' folder at the end that pulls the next fragment."""
    xbmcplugin.setContent(HANDLE, "songs")
    try:
        _, tracks = with_client(lambda c: c.get_fragment(station_id, is_start))
    except PandoraError as e:
        notify(str(e), error=True)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    if not tracks:
        notify("Pandora returned no tracks for this station", error=True)

    limit = ADDON.getSettingInt("tracks_per_page") or 4
    for track in tracks[:limit]:
        audio_url = track.get("audioURL")
        if not audio_url:
            continue
        li = track_listitem(track)
        # Pass the resolved stream directly; Pandora URLs expire, so these
        # items are meant to be played from this listing, not bookmarked.
        xbmcplugin.addDirectoryItem(HANDLE, audio_url, li, isFolder=False)

    more = xbmcgui.ListItem(label="[B]More…[/B]")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="station", station_id=station_id, start="0"),
        more,
        isFolder=True,
    )
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def do_search() -> None:
    query = xbmcgui.Dialog().input("Search Pandora (artist or song)")
    if not query:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    try:
        _, results = with_client(lambda c: c.search(query))
    except PandoraError as e:
        notify(str(e), error=True)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    xbmcplugin.setPluginCategory(HANDLE, "Search: %s" % query)
    for r in results:
        pid = r.get("pandoraId")
        if not pid:
            continue
        rtype = r.get("type", "")
        if rtype == "AR":
            label = "%s  [COLOR gray](artist)[/COLOR]" % r.get("name", "Unknown")
        elif rtype == "TR":
            label = "%s - %s  [COLOR gray](song)[/COLOR]" % (
                r.get("artistName", ""), r.get("name", "Unknown"))
        else:
            continue
        li = xbmcgui.ListItem(label=label)
        icon = r.get("icon", {}).get("artUrl") or r.get("art", [{}])[-1].get("url")
        if icon:
            if icon.startswith("/"):
                icon = "https://content-images.p-cdn.com" + icon
            li.setArt({"thumb": icon, "icon": icon})
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="create_station", pandora_id=pid, query=query),
            li,
            isFolder=True,
        )
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def do_create_station(pandora_id: str, query: str) -> None:
    try:
        _, station = with_client(lambda c: c.create_station(pandora_id, query))
    except PandoraError as e:
        notify(str(e), error=True)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    name = station.get("name", "station")
    notify('Created "%s"' % name)
    station_id = station.get("stationId")
    if station_id:
        # Jump straight into the new station.
        list_station_tracks(station_id, True)
    else:
        xbmcplugin.endOfDirectory(HANDLE)


def do_delete_station(station_id: str, name: str) -> None:
    if not xbmcgui.Dialog().yesno("Pandora", 'Delete station "%s"?' % name):
        return
    try:
        with_client(lambda c: c.delete_station(station_id))
        notify('Deleted "%s"' % name)
        xbmc.executebuiltin("Container.Refresh")
    except PandoraError as e:
        notify(str(e), error=True)


def do_feedback(token: str, positive: bool) -> None:
    try:
        with_client(lambda c: c.feedback(token, positive))
        notify("Thumbs up saved" if positive else "Thumbs down saved")
    except PandoraError as e:
        notify(str(e), error=True)


def do_test_login() -> None:
    """Settings-page test button: force a fresh login, bypassing any cached
    session, and report the result in a dialog."""
    dialog = xbmcgui.Dialog()
    username = ADDON.getSetting("username").strip()
    password = ADDON.getSetting("password")
    if not username or not password:
        dialog.ok("Pandora", "Enter your email and password first.")
        return

    clear_session()           # a stale cached session must not mask a failure
    client = PandoraClient()  # deliberately NOT make_client(): no restore
    try:
        login(client, username, password)
    except PandoraError as e:
        dialog.ok("Pandora", "Login failed:[CR]%s" % e)
        return
    except Exception as e:
        xbmc.log("plugin.audio.pandora: test login error: %s" % e, xbmc.LOGWARNING)
        dialog.ok("Pandora", "Login test error:[CR]%s" % e)
        return
    dialog.ok("Pandora", "Login OK.[CR]Session established and cached.")


def router(paramstring: str) -> None:
    params = dict(urllib.parse.parse_qsl(paramstring))
    action = params.get("action")

    if action is None:
        list_stations()
    elif action == "station":
        list_station_tracks(params["station_id"], params.get("start") == "1")
    elif action == "search":
        do_search()
    elif action == "create_station":
        do_create_station(params["pandora_id"], params.get("query", ""))
    elif action == "delete_station":
        do_delete_station(params["station_id"], params.get("name", "this station"))
    elif action == "feedback":
        do_feedback(params["token"], params.get("positive") == "1")
    elif action == "test_login":
        do_test_login()
    elif action == "logout":
        clear_session()
        notify("Session cleared")
    else:
        xbmc.log("plugin.audio.pandora: unknown action %s" % action, xbmc.LOGWARNING)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router(sys.argv[2][1:])
