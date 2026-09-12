# -*- coding: utf-8 -*-
"""Two helpers for the "this call may not exist on every build" guards.

Most of those guards are written as ``with suppress(Exception):`` where the
code simply carries on. This module covers the rest: a loop that has to skip
the current item when the call did not go through, where ``suppress`` would let
the code below run on a value that was never produced.

``attempt`` is for a call whose result is wanted, ``tried`` for one that is only
performed for its effect::

    properties = attempt(symbol_layer.properties)
    if not properties:
        continue

    if not tried(geometry.transform, transform):
        continue

Both swallow every exception on purpose. The calls behind them are QGIS API
that a 3.28 build may not carry, or a transform that a point outside the
projection's domain refuses; neither is an error the user can act on.
"""


def attempt(func, *args, **kwargs):
    """``func(*args, **kwargs)``, or ``None`` when the call does not go through."""
    try:
        return func(*args, **kwargs)
    except Exception:
        return None


def tried(func, *args, **kwargs):
    """``True`` when ``func(*args, **kwargs)`` went through, ``False`` when not."""
    try:
        func(*args, **kwargs)
    except Exception:
        return False
    return True
