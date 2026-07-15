from __future__ import annotations

import http.cookiejar
import sys
import time
import urllib.parse
from typing import Any, Callable, TypeVar

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

_WINDOW_MUSIC_PLAYLIST = 10500

sys.path.insert(0, xbmcaddon.Addon().getAddonInfo("path") + "/resources/lib")
from pandora import PandoraClient, PandoraError  # noqa: E402

ADDON = xbmcaddon.Addon()
HANDLE: int = int(sys.argv[1])
BASE_URL: str = sys.argv[0]

# One client per plugin invocation. The auth token is cached in a window
# property so we don't log in on every directory listing / track fetch.
_PROP_TOKEN = "plugin.audio.pandora.authtoken"
_PROP_CSRF = "plugin.audio.pandora.csrf"

# Continuation plumbing (shared with service.py):
#   pandora.station_id  - station whose tracks are queued; service passes it
#                         back to action=append when the queue runs low.
#   pandora.appending   - re-entrancy guard so overlapping RunPlugin(append)
#                         invocations are silent no-ops.
_PROP_STATION = "pandora.station_id"
_PROP_APPENDING = "pandora.appending"

# Backoff schedule for transient fragment-fetch failures (seconds).
# Worst case adds ~1.75s before giving up -- within the agreed budget.
_RETRY_DELAYS: tuple[float, ...] = (0.25, 0.5, 1.0)

T = TypeVar("T")


_T0 = time.time()


def trace(msg: str) -> None:
    xbmc.log("[plugin.audio.pandora/plugin] (+%6.2fs h=%s) %s"
             % (time.time() - _T0, HANDLE, msg), xbmc.LOGINFO)


def build_url(**kwargs: str) -> str:
    return BASE_URL + "?" + urllib.parse.urlencode(kwargs)


def notify(message: str, error: bool = False) -> None:
    icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
    xbmcgui.Dialog().notification("Pandora", message, icon, 4000)


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


def with_retry(fn: Callable[[PandoraClient], T]) -> tuple[PandoraClient, T]:
    """with_client plus short exponential backoff for transient failures.

    with_client already handles the auth-expiry case (clear + relogin, once).
    This layer covers everything else transient -- DNS hiccups, CDN/API
    timeouts -- with delays of 0.25s / 0.5s / 1s before the error propagates.
    """
    delays = list(_RETRY_DELAYS)
    attempt = 1
    while True:
        try:
            return with_client(fn)
        except PandoraError as e:
            if not delays:
                trace("with_retry: attempt %d failed (%s) -> giving up" % (attempt, e))
                raise
            delay = delays.pop(0)
            trace("with_retry: attempt %d failed (%s) -> retrying in %.2fs"
                  % (attempt, e, delay))
            xbmc.sleep(int(delay * 1000))
            attempt += 1


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
        li.getMusicInfoTag().setTitle(name)
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
        tag.setArtist(artist)
    if album:
        tag.setAlbum(album)
    tag.setDuration(int(track.get("trackLength", 0)))
    if int(track.get("rating", 0)) > 0:
        tag.setUserRating(10)          # Pandora thumbs-up -> max user rating

    art_url = track.get("albumArt", [])
    if art_url:
        url = art_url[-1].get("url")
        if url:
            li.setArt({"thumb": url, "icon": url})
    li.setProperty("IsPlayable", "true")
    token = track.get("trackToken")
    if token:
        li.setProperty("pandora_token", token)
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
    limit = ADDON.getSettingInt("tracks_per_page") or 4
    trace("list_station_tracks: station=%s start=%r limit=%s"
          % (station_id, is_start, limit))

    xbmcplugin.setContent(HANDLE, "songs")
    try:
        _, tracks = with_retry(lambda c: c.get_fragment(station_id, is_start))
    except PandoraError as e:
        notify(str(e), error=True)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    if not tracks:
        notify("Pandora returned no tracks for this station", error=True)

    # Remember which station is on deck so the service can pass it back to
    # action=append when the player playlist is about to run dry.
    xbmcgui.Window(10000).setProperty(_PROP_STATION, station_id)

    added = 0
    skipped = 0
    for idx, track in enumerate(tracks[:limit]):
        audio_url = track.get("audioURL")
        title = track.get("songTitle", "?")
        token = track.get("trackToken", "")

        if not audio_url:
            skipped += 1
            trace("  [%02d] SKIP (no audioURL): %s" % (idx, title))
            continue
        trace("  [%02d] add: %s | token=%s | url=...%s"
              % (idx, title, (token[:10] + "...") if token else "MISSING",
                 audio_url[-40:]))

        li = track_listitem(track)
        # Pass the resolved stream directly; Pandora URLs expire, so these
        # items are meant to be played from this listing, not bookmarked.
        xbmcplugin.addDirectoryItem(HANDLE, audio_url, li, isFolder=False)
        added += 1
    trace("directory built: added=%d skipped=%d" % (added, skipped))

    more = xbmcgui.ListItem(label="[B]More…[/B]")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="station", station_id=station_id, start="0"),
        more,
        isFolder=True,
    )
    trace("endOfDirectory(succeeded=True)")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def do_append(station_id: str) -> None:
    """Fetch the next playlist fragment and append its tracks to the music
    player playlist. Invoked by the service via RunPlugin when the queue is
    about to run dry -- this is what keeps a station playing hands-off.

    RunPlugin invocations don't render a directory, so no endOfDirectory.
    Guarded against re-entry: overlapping invocations are silent no-ops.
    """
    win = xbmcgui.Window(10000)
    if win.getProperty(_PROP_APPENDING) == "1":
        trace("append: already in progress, skipping")
        return
    win.setProperty(_PROP_APPENDING, "1")
    try:
        limit = ADDON.getSettingInt("tracks_per_page") or 4
        trace("append: station=%s limit=%s" % (station_id, limit))
        try:
            _, tracks = with_retry(lambda c: c.get_fragment(station_id, False))
        except PandoraError as e:
            trace("append: fragment fetch failed after retries: %s" % e)
            notify(str(e), error=True)
            return

        playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        added = 0
        skipped = 0
        for idx, track in enumerate(tracks[:limit]):
            audio_url = track.get("audioURL")
            title = track.get("songTitle", "?")
            token = track.get("trackToken", "")

            if not audio_url:
                skipped += 1
                trace("  [%02d] SKIP (no audioURL): %s" % (idx, title))
                continue
            trace("  [%02d] append: %s | token=%s | url=...%s"
                  % (idx, title, (token[:10] + "...") if token else "MISSING",
                     audio_url[-40:]))
            playlist.add(audio_url, track_listitem(track))
            added += 1

        trace("append done: added=%d skipped=%d playlist size=%d"
              % (added, skipped, playlist.size()))
        if added and xbmcgui.getCurrentWindowId() == _WINDOW_MUSIC_PLAYLIST:
            win = xbmcgui.Window(_WINDOW_MUSIC_PLAYLIST)
            list_id = win.getFocusId()
            pos = playlist.getposition()
            trace("append: playlist window active -> Container.Refresh "
                "(focused control=%d, playing pos=%d)" % (list_id, pos))
            xbmc.executebuiltin("Container.Refresh")
            if list_id > 0 and pos >= 0:
                xbmc.sleep(300)  # let the container reload before refocusing
                xbmc.executebuiltin(
                    "SetFocus(%d, %d, absolute)" % (list_id, pos))
        if not added:
            notify("Pandora returned no playable tracks", error=True)
    finally:
        win.clearProperty(_PROP_APPENDING)


def do_search() -> None:
    query = xbmcgui.Dialog().input("Search Pandora (artist or song)")
    if not query:
        trace("endOfDirectory(succeeded=False)")
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    try:
        _, results = with_client(lambda c: c.search(query))
    except PandoraError as e:
        notify(str(e), error=True)
        trace("endOfDirectory(succeeded=False)")
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
    trace("endOfDirectory(succeeded=True)")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def do_create_station(pandora_id: str, query: str) -> None:
    try:
        _, station = with_client(lambda c: c.create_station(pandora_id, query))
    except PandoraError as e:
        notify(str(e), error=True)
        trace("endOfDirectory(succeeded=False)")
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    name = station.get("name", "station")
    notify('Created "%s"' % name)
    station_id = station.get("stationId")
    if station_id:
        # Jump straight into the new station.
        list_station_tracks(station_id, True)
    else:
        trace("endOfDirectory(succeeded=True)")
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
    trace("feedback: token=%s positive=%s" % (token[:10], positive))
    try:
        with_client(lambda c: c.feedback(token, positive))
        notify("Thumbs up saved" if positive else "Thumbs down saved")
        trace("feedback: API ok")
    except PandoraError as e:
        trace(f"feedback: API error{e}")
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
    trace("router: argv=%r params=%r" % (sys.argv, paramstring))
    params = dict(urllib.parse.parse_qsl(paramstring))
    action = params.get("action")

    if action is None:
        list_stations()
    elif action == "station":
        list_station_tracks(params["station_id"], params.get("start") == "1")
    elif action == "append":
        do_append(params["station_id"])
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
        trace("endOfDirectory(succeeded=False)")
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router(sys.argv[2][1:])
