# -*- coding: utf-8 -*-
"""Symbol Bag – small sound effects (the Genie whoosh).

Windows: the standard library's winsound (always available, plays async).
Elsewhere: Qt's QSoundEffect when QtMultimedia is installed, else the
system player (afplay on macOS, paplay / aplay on Linux). Any failure is
silent – a missing sound must never disturb the work.
"""

from contextlib import suppress
import array
import os
import shutil
import sys
import tempfile
import wave

SOUND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sounds")

# (key, menu label, one-line description)
STYLES = [
    ("whoosh", "Whoosh", "a soft rush of air"),
    ("bubble", "Bubble", "water bubbles"),
    ("chime", "Chime", "two soft bell notes"),
    ("magic", "Magic", "a sparkling glissando"),
]
STYLE_KEYS = [k for k, _l, _d in STYLES]

_effects = {}
played = []          # names played (kept short; handy for diagnostics and tests)


def path_of(name):
    return os.path.join(SOUND_DIR, name + ".wav")


def event_file(event, style):
    """'in' (minimize) / 'out' (restore) in a style -> sound name."""
    style = style if style in STYLE_KEYS else "whoosh"
    return "{}_{}".format(style, event)


def play_event(event, style, volume=0.7):
    return play(event_file(event, style), volume)


def scaled_path(name, volume):
    """The sound at `volume` (0–1). winsound has no volume control, so the
    samples themselves are scaled once and cached (10 % steps)."""
    src = path_of(name)
    v = max(0, min(10, int(round(volume * 10))))
    if v >= 10:
        return src
    folder = os.path.join(tempfile.gettempdir(), "symbol_bag_sounds")
    dst = os.path.join(folder, "{}_{}.wav".format(name, v * 10))
    try:
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
        os.makedirs(folder, exist_ok=True)
        with wave.open(src, "rb") as f:
            params = f.getparams()
            data = array.array("h", f.readframes(f.getnframes()))
        if sys.byteorder == "big":
            data.byteswap()
        gain = (v / 10.0) ** 1.6              # perceptual curve: 50 % sounds like half as loud
        data = array.array("h", (int(x * gain) for x in data))
        if sys.byteorder == "big":
            data.byteswap()
        tmp = dst + ".tmp"
        with wave.open(tmp, "wb") as f:
            f.setparams(params)
            f.writeframes(data.tobytes())
        os.replace(tmp, dst)
        return dst
    except Exception:
        return src


def play(name, volume=0.7):
    if volume <= 0.001 or not os.path.isfile(path_of(name)):
        return False
    path = scaled_path(name, volume)
    played.append(name)
    del played[:-20]
    with suppress(Exception):
        if sys.platform.startswith("win"):
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return True
    with suppress(Exception):
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtMultimedia import QSoundEffect
        eff = _effects.get(name)
        if eff is None:
            eff = QSoundEffect()
            eff.setSource(QUrl.fromLocalFile(path))
            _effects[name] = eff
        eff.setVolume(1.0)                     # the file is already at the chosen volume
        eff.play()
        return True
    for player in (["afplay"], ["paplay"], ["aplay", "-q"]):
        exe = shutil.which(player[0])
        if exe:
            try:
                from qgis.PyQt.QtCore import QProcess
                if QProcess.startDetached(exe, player[1:] + [path]):
                    return True
            except Exception:
                return False
    return False


def stop_all():
    with suppress(Exception):
        if sys.platform.startswith("win"):
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
    for eff in _effects.values():
        with suppress(Exception):
            eff.stop()
