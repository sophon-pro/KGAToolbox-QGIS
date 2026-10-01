# -*- coding: utf-8 -*-
"""File-name and folder checks for the output. Pure Python."""

import os
import re

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM%d' % i for i in range(1, 10)} \
    | {'LPT%d' % i for i in range(1, 10)}
MAX_NAME = 120


def name_problem(name):
    """Why `name` cannot be used as a file name on Windows, or None."""
    if not name or not name.strip():
        return 'Enter a file name.'
    if _BAD.search(name):
        return 'The file name cannot contain  < > : " / \\ | ? *'
    if name != name.rstrip(' .'):
        return 'The file name cannot end with a space or a dot.'
    if name.split('.')[0].upper() in _RESERVED:
        return '"%s" is a reserved name on Windows.' % name
    if len(name) > MAX_NAME:
        return 'The file name is too long.'
    return None


def sanitize_name(name):
    """A safe version of `name` (Khmer and other Unicode letters are kept)."""
    name = _BAD.sub('_', name or '').strip().rstrip('. ')
    if name.split('.')[0].upper() in _RESERVED:
        name = '_' + name
    return name[:MAX_NAME] or 'imagery'


def folder_problem(folder):
    """Why `folder` cannot be written to, or None."""
    if not folder:
        return 'Choose an output folder.'
    if os.path.exists(folder) and not os.path.isdir(folder):
        return 'The output path is not a folder.'
    probe = folder
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return 'The output folder does not exist.'
        probe = parent
    if not os.access(probe, os.W_OK):
        return 'The output folder is not writable.'
    return None
