# -*- coding: utf-8 -*-
"""The field-boundary models: which ones, where they live, checking them.

Both are Delineate Anything models (Lavreniuk et al.), YOLO instance
segmentation networks trained on field boundaries from imagery of 0.25 to
10 m per pixel. They were published as PyTorch checkpoints; the ONNX files
here were exported from those with Ultralytics (512 x 512 input, opset 17) and
are attached to a release of the KGA Toolbox repository, so the plugin needs
neither PyTorch nor Ultralytics. Each is pinned by size and SHA-256.

The AI runtime they run on is the road tool's (``core.roads.runtime``): one
onnxruntime install serves both.

A model can also be loaded from a file the user already has (a copy on a
shared drive, an offline PC): the file is checked against the same hash and
copied into the profile.
"""

import hashlib
import os
import shutil

from ..roads import runtime as RT

#: Where the ONNX exports are published (a GitHub release of the plugin).
RELEASE = ('https://github.com/sophon-pro/KGAToolbox-QGIS/releases/download/'
           'paddy-models-v1/')

_CREDIT = (
    'Field model: Delineate Anything by M. Lavreniuk et al. ("Delineate '
    'Anything: Resolution-Agnostic Field Boundary Delineation on '
    'Satellite Imagery", arXiv:2504.02534; v2: '
    'arXiv:2607.19069), https://github.com/Lavreniuk/Delineate-Anything, '
    'AGPL-3.0. A YOLO instance-segmentation network trained on the FBIS '
    'field-boundary dataset. Converted to ONNX for KGA Toolbox without '
    'changing the weights.')

#: The models, smallest first. 'sha256' and 'bytes' pin the published files.
MODELS = (
    {
        'id': 'da-small',
        'title': 'Delineate Anything S - small, fast',
        'short': 'Delineate Anything S',
        'file': 'DelineateAnything-S.onnx',
        'url': RELEASE + 'DelineateAnything-S.onnx',
        'page': 'https://huggingface.co/MykolaL/DelineateAnything',
        'sha256': ('5a956ce18515e8a09911ad8e0766ff72'  # pragma: allowlist secret
                   '6d2687e04d8daacd9a5f15456edabbee'),  # pragma: allowlist secret
        'bytes': 11566483,
        'chip': 512,
        'licence': 'AGPL-3.0',
        'credit': _CREDIT,
        'about': 'About 4x faster on a CPU. In tests it did best on large '
                 'blocks of paddy (Battambang).',
    },
    {
        'id': 'da-v2',
        'title': 'Delineate Anything v2 - large, slower',
        'short': 'Delineate Anything v2',
        'file': 'DelineateAnythingv2.onnx',
        'url': RELEASE + 'DelineateAnythingv2.onnx',
        'page': 'https://huggingface.co/MykolaL/DelineateAnything',
        'sha256': ('8456ef5b8deb89d2037fa61b5ca3e408'  # pragma: allowlist secret
                   'd65d97148a26ab81f9f2a3de70b07e9c'),  # pragma: allowlist secret
        'bytes': 248344426,
        'chip': 512,
        'licence': 'AGPL-3.0',
        'credit': _CREDIT,
        'about': 'Trained on 73 million fields in 61 countries. In tests it '
                 'separated more small plots among villages (Takeo).',
    },
)
DEFAULT_MODEL = 'da-small'


class ModelProblem(RT.RuntimeProblem):
    """The model file is missing or not the expected one. Shown as-is."""


def model(ident):
    for entry in MODELS:
        if entry['id'] == ident:
            return entry
    return MODELS[0]


def model_path(entry):
    return os.path.join(RT.models_dir(), entry['file'])


def megabytes(entry):
    return max(1, int(round(entry['bytes'] / 1e6)))


def present(entry):
    """True when the file is there at the published size (cheap check)."""
    path = model_path(entry)
    return os.path.isfile(path) and (
        not entry['bytes'] or os.path.getsize(path) == entry['bytes'])


def _digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def check_file(path, entry):
    """Raise ModelProblem unless `path` is the model, byte for byte."""
    if not os.path.isfile(path):
        raise ModelProblem('The field model has not been downloaded yet.')
    if _digest(path) != entry['sha256']:
        raise ModelProblem(
            'The field model file is damaged or not the expected one '
            '({0}). Download it again.'.format(entry['file']))


def import_file(source, entry):
    """Copy a model file the user picked into the profile, once checked."""
    check_file(source, entry)
    target = model_path(entry)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.normcase(os.path.abspath(source)) != \
            os.path.normcase(os.path.abspath(target)):
        shutil.copyfile(source, target)
    return target


def status(entry):
    """The runtime's state (see roads.runtime.status) plus this model's."""
    state = RT.status()
    state['model'] = present(entry)
    return state
