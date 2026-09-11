"""Shared, GUI-free logic for the KGA Toolbox algorithms.

Nothing is imported here on purpose. `provider.py` walks `algorithms/` at
startup and imports every module it finds; those modules import from this
package, so keeping `core/__init__` empty keeps plugin start-up cheap and
avoids dragging Qt widgets into a head-less Processing run.
"""
