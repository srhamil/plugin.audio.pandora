"""Background service for plugin.audio.pandora.  DIAGNOSTIC BUILD.

Same behavior as service.py plus verbose instrumentation at INFO level so
events show up without enabling component-specific debug logging.  Once the
hint problem is found, drop the noisy lines back to LOGDEBUG or revert.

This build adds:
  * session detection (first Pandora track after idle = session start)
  * jump-to-window on session start (start_view setting: stay / playlist /
    fullscreen), performed BEFORE the hint since the modal hint dialog
    blocks window activations (seen in the log: "Activate of window '12006'
    refused because there are active modal dialogs")
  * queue continuation: when the last queued track starts, ask default.py
    (RunPlugin action=append) to fetch the next fragment and top up the
    player playlist -- this replaces reliance on the GUI "More..." item,
    which never enters the player queue and so cannot continue playback
  * best-effort recovery in onPlayBackError: resume at the next queued track
  * hint gating: the overlay is only shown while the fullscreen
    visualisation (window 12006) is active, because that is the only window
    the CEC color-key keymap is scoped to.  hint_pending persists until the
    user reaches fullscreen or the next track replaces it.
"""
from __future__ import annotations

import os

import xbmc
import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
ADDON_PATH = ADDON.getAddonInfo("path")

HOME = xbmcgui.Window(10000)

PROP_TOKEN = "pandora.current_token"
PROP_RATED = "pandora.rated"
PROP_STATION = "pandora.station_id"   # set by default.py list_station_tracks
ITEM_PROP_TOKEN = "pandora_token"

FULLSCREEN_VIS_ID = 12006             # music fullscreen visualisation window


HINT_IMAGE = os.path.join(ADDON_PATH, "resources", "media", "hint.png")
HINT_W = 400
HINT_H = 80
HINT_INSET = 40  # base inset from anchored screen edges

# Anchor index matches the order of the position enum in settings.xml:
# 0 top-left, 1 top-center, 2 top-right,
# 3 center-left, 4 center, 5 center-right,
# 6 bottom-left, 7 bottom-center, 8 bottom-right
def hint_geometry() -> tuple[int, int]:
    try:
        anchor = int(ADDON.getSetting("hint_position"))
    except ValueError:
        anchor = 5  # center-right
    try:
        dx = int(ADDON.getSetting("hint_offset_x"))
    except ValueError:
        dx = 0
    try:
        dy = int(ADDON.getSetting("hint_offset_y"))
    except ValueError:
        dy = 0

    col = anchor % 3   # 0 left, 1 center, 2 right
    row = anchor // 3  # 0 top, 1 center, 2 bottom

    if col == 0:
        x = HINT_INSET
    elif col == 1:
        x = (1280 - HINT_W) // 2
    else:
        x = 1280 - HINT_W - HINT_INSET

    if row == 0:
        y = HINT_INSET
    elif row == 1:
        y = (720 - HINT_H) // 2
    else:
        y = 720 - HINT_H - HINT_INSET

    x = max(0, min(1280 - HINT_W, x + dx))
    y = max(0, min(720 - HINT_H, y + dy))

    return x, y


def hint_seconds() -> float:
    try:
        return float(ADDON.getSetting("hint_seconds"))
    except ValueError:
        return 5.0


def log(msg: str, level: int = xbmc.LOGINFO) -> None:
    xbmc.log(f"[{ADDON_ID}/service] {msg}", level)


def clear_props(reason: str) -> None:
    log(f"clearing window properties ({reason})")
    HOME.clearProperty(PROP_TOKEN)
    HOME.clearProperty(PROP_RATED)


class HintOverlay(xbmcgui.WindowDialog):
    def __init__(self) -> None:
        super().__init__()
        x, y = hint_geometry()
        self.addControl(
            xbmcgui.ControlImage(x, y, HINT_W, HINT_H, HINT_IMAGE)
        )


class PandoraPlayer(xbmc.Player):
    def __init__(self) -> None:
        super().__init__()
        self.hint_pending: bool = False
        # True while a Pandora session is running.  Set on the first Pandora
        # track after idle (that is the moment we jump to the configured
        # window); auto-advanced tracks keep it True so they never re-jump.
        self.session_active: bool = False

    # -- callbacks -------------------------------------------------------

    def onPlayBackStarted(self) -> None:
        log("onPlayBackStarted fired")

    def onAVChange(self) -> None:
        log("onAVChange fired")

    def onAVStarted(self) -> None:
        log("onAVStarted fired")
        try:
            fname = self.getPlayingFile()
        except RuntimeError:
            fname = "<nothing playing>"
        log(f"  playing file: {fname[:120]}")

        token = self._playing_token()
        infolabel = xbmc.getInfoLabel(f"MusicPlayer.Property({ITEM_PROP_TOKEN})")
        log(f"  token via getPlayingItem: {token!r:.40}")
        log(f"  token via InfoLabel:      {infolabel!r:.40}")

        if not token and infolabel:
            log("  getPlayingItem property empty, falling back to InfoLabel")
            token = infolabel

        if not token:
            log("  no Pandora token found -> treating as non-Pandora playback")
            clear_props("non-Pandora item started")
            self.session_active = False
            return
        if HOME.getProperty(PROP_TOKEN) == token:
            log("  same token as before (seek/re-announce), ignoring")
            return
        HOME.setProperty(PROP_TOKEN, token)
        HOME.clearProperty(PROP_RATED)

        # -- session start: jump to the configured window.  Must happen
        #    before hint_pending is set: the hint dialog is modal and would
        #    block the window activation.
        if not self.session_active:
            self.session_active = True
            target = ADDON.getSetting("start_view")
            log(f"  session start (start_view={target!r})")
            if target == "1":
                log("  jumping to playlist window")
                xbmc.executebuiltin("ActivateWindow(musicplaylist)")
            elif target == "2":
                log("  jumping to fullscreen visualisation")
                xbmc.executebuiltin("ActivateWindow(visualisation)")

        # -- continuation: when the LAST queued track starts, top up the
        #    queue.  Fetching at last-track START gives a full track-length
        #    of headroom while keeping the expiring CDN URLs fresh.
        playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        pos, size = playlist.getposition(), playlist.size()
        log(f"  queue position {pos + 1} of {size}")
        if size and pos >= size - 1:
            station_id = HOME.getProperty(PROP_STATION)
            if station_id:
                log(f"  last track in queue -> requesting append for station {station_id}")
                xbmc.executebuiltin(
                    "RunPlugin(plugin://plugin.audio.pandora/"
                    "?action=append&station_id=%s)" % station_id)
            else:
                log("  last track in queue but no station_id property -- cannot append")

        self.hint_pending = True
        log(f"  token published, hint_pending=True ({token[:12]}...)")

    def onPlayBackStopped(self) -> None:
        log("onPlayBackStopped fired")
        clear_props("playback stopped")
        self.session_active = False

    def onPlayBackEnded(self) -> None:
        log("onPlayBackEnded fired")
        clear_props("playback ended")
        self.session_active = False

    def onPlayBackError(self) -> None:
        # Best-effort recovery: if a Pandora queue item failed (expired CDN
        # URL, network blip), try to resume at the next queued track instead
        # of letting the session die.  Heavily logged because we have never
        # captured this failure mode live -- the next real failure tells us
        # whether this approach works or needs rethinking.
        log("onPlayBackError fired")
        was_active = self.session_active
        clear_props("playback error")
        if not was_active:
            return
        playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        pos, size = playlist.getposition(), playlist.size()
        log(f"  error at queue position {pos + 1} of {size}")
        if 0 <= pos < size - 1:
            log("  attempting to resume at next queued track")
            # session_active stays True: if the resume works, the next
            # onAVStarted must NOT be treated as a new session (no re-jump).
            xbmc.Player().play(playlist, startpos=pos + 1)
        else:
            log("  no next track to resume with, session over")
            self.session_active = False

    # -- helpers ---------------------------------------------------------

    def _playing_token(self) -> str:
        try:
            item = self.getPlayingItem()
        except RuntimeError as e:
            log(f"  getPlayingItem raised: {e}")
            return ""
        return item.getProperty(ITEM_PROP_TOKEN) or ""


def run() -> None:
    monitor = xbmc.Monitor()
    player = PandoraPlayer()
    clear_props("service start")

    log("=== service started (diagnostic build) ===")
    log(f"addon path: {ADDON_PATH}")
    log(f"hint image: {HINT_IMAGE} exists={os.path.exists(HINT_IMAGE)}")

    while not monitor.abortRequested():
        # Hint gating: only show over the fullscreen visualisation, the one
        # window where the CEC color keys actually do anything.  A pending
        # hint waits here until the user reaches fullscreen; the next track
        # simply re-arms it, so staleness is bounded to one track.
        if player.hint_pending:
            if xbmcgui.getCurrentWindowId() == FULLSCREEN_VIS_ID:
                player.hint_pending = False
                log("showing hint overlay")
                overlay = HintOverlay()
                overlay.show()
                aborted = monitor.waitForAbort(hint_seconds())
                overlay.close()
                del overlay
                log("hint overlay closed")
                if aborted:
                    break
        if monitor.waitForAbort(0.5):
            break

    clear_props("service shutdown")
    log("=== service stopped ===")


if __name__ == "__main__":
    run()
