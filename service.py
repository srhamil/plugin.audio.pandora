"""Background service for plugin.audio.pandora.  DIAGNOSTIC BUILD.

Same behavior as service.py plus verbose instrumentation at INFO level so
events show up without enabling component-specific debug logging.  Once the
hint problem is found, drop the noisy lines back to LOGDEBUG or revert.
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
ITEM_PROP_TOKEN = "pandora_token"


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
            return
        if HOME.getProperty(PROP_TOKEN) == token:
            log("  same token as before (seek/re-announce), ignoring")
            return
        HOME.setProperty(PROP_TOKEN, token)
        HOME.clearProperty(PROP_RATED)
        self.hint_pending = True
        log(f"  token published, hint_pending=True ({token[:12]}...)")

    def onPlayBackStopped(self) -> None:
        log("onPlayBackStopped fired")
        clear_props("playback stopped")

    def onPlayBackEnded(self) -> None:
        log("onPlayBackEnded fired")
        clear_props("playback ended")

    def onPlayBackError(self) -> None:
        log("onPlayBackError fired")
        clear_props("playback error")

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
        if player.hint_pending:
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
