# -*- coding: utf-8 -*-
"""onnxruntime and the road model: where they live, installing, loading.

Neither ships with QGIS or with the plugin. onnxruntime is a compiled library
(about 40 MB), and the model's weights are about 100 MB, so both are fetched
once, on request, into the QGIS profile:

    <profile>/python/kga_ml/       onnxruntime, installed by pip --target
    <profile>/kga_models/          the model file

Installing into a folder of our own, with ``--no-deps``, means nothing QGIS
already has (numpy above all) is replaced. onnxruntime imports with numpy alone.

**The version is pinned on Windows.** QGIS for Windows bundles its own copy of
the Microsoft C++ runtime (msvcp140.dll), older than the one onnxruntime 1.21
and later are built against: 1.22 fails to load, and 1.32 crashes the whole
process on import. 1.20.1 loads and runs (checked in QGIS 3.44 and 4.2, Python
3.12). Because a bad onnxruntime takes QGIS down rather than raising, an
onnxruntime this module did not install itself is never imported straight away:
it is imported once in a separate Python process first, and only a version that
survived that is imported here.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import hashlib
import importlib.util
import json
import os
import sys
from contextlib import suppress

from qgis.core import QgsApplication, QgsSettings

#: The model. One for now; the dict keeps what the engine needs to know about
#: it beside who made it.
MODEL = {
    'id': 'fair-unet-roads',
    'title': 'fAIr U-Net roads (ResNet-34)',
    'file': 'unet-roads.onnx',
    # Pinned to one commit, so a later upload cannot change what runs here.
    'url': 'https://huggingface.co/Rafael990/unet-roads/resolve/'
           'fe1bed259c13ad8558775abf026af29287299743/unet-roads.onnx',
    'page': 'https://huggingface.co/Rafael990/unet-roads',
    'sha256': 'd72390ebedd8cf2477c483a2e12171f58c01b0a3f6acb2e9e7178d0ceb712cc8',  # pragma: allowlist secret
    'bytes': 97966820,
    'input': 'image',
    'chip': 256,
    # Metres per pixel the model was trained at.
    'gsd': 0.3,
    # The training labels were centrelines buffered to 6 m, so the mask comes
    # out about that wide whatever the real road is.
    'label_width_m': 6.0,
    'licence': 'CC BY-SA 4.0',
    'credit': (
        'Road model: "unet-roads", a fAIr base model by Rafael990 on Hugging '
        'Face (CC BY-SA 4.0). U-Net with a ResNet-34 encoder, built with '
        'segmentation-models-pytorch. Trained on SpaceNet 3 (Van Etten et al., '
        '2018, arXiv:1807.01232; CC BY-SA 4.0), OpenAerialMap imagery (CC BY '
        '4.0) and OpenStreetMap highways (© OpenStreetMap contributors, '
        'ODbL).'),
}

#: onnxruntime to install, by platform. None means "the newest pip offers".
PINNED = '1.20.1'

SETTINGS_PROBE = 'kga/roads/runtime_probe'


class RuntimeProblem(Exception):
    """onnxruntime or the model is missing or unusable. Shown as-is."""


# ------------------------------------------------------------------- places

def profile_dir():
    return QgsApplication.qgisSettingsDirPath()


def libs_dir():
    return os.path.join(profile_dir(), 'python', 'kga_ml')


def models_dir():
    return os.path.join(profile_dir(), 'kga_models')


def model_path(model=MODEL):
    return os.path.join(models_dir(), model['file'])


def python_exe():
    """The Python that QGIS runs on, for pip and for the import probe.

    ``sys.executable`` is the QGIS program itself inside the desktop app, so the
    interpreter is looked for beside the Python library instead.
    """
    names = ('python.exe', 'python3.exe') if os.name == 'nt' else \
        ('python3', 'python')
    folders = [sys.exec_prefix, os.path.join(sys.exec_prefix, 'bin'),
               os.path.dirname(sys.executable)]
    for folder in folders:
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path
    return None


def pip_arguments():
    """``python -m pip ...`` arguments that install onnxruntime into libs_dir."""
    package = 'onnxruntime'
    if os.name == 'nt' and sys.version_info[:2] <= (3, 12):
        package = 'onnxruntime=={0}'.format(PINNED)
    return ['-m', 'pip', 'install', '--upgrade', '--no-deps',
            '--disable-pip-version-check', '--no-input', '--target',
            libs_dir(), package]


def install_command():
    """The same install as a command line a user can copy into a shell."""
    exe = python_exe() or 'python'
    parts = [exe] + pip_arguments()
    return ' '.join('"{0}"'.format(p) if ' ' in p else p for p in parts)


# ------------------------------------------------------------- onnxruntime

def _add_libs_path():
    folder = libs_dir()
    if os.path.isdir(folder) and folder not in sys.path:
        sys.path.insert(0, folder)


def _spec_location():
    """(folder, version) of the onnxruntime Python would import, unimported."""
    _add_libs_path()
    spec = importlib.util.find_spec('onnxruntime')
    if spec is None or not spec.origin:
        return None, None
    folder = os.path.dirname(spec.origin)
    version = ''
    parent = os.path.dirname(folder)
    with suppress(OSError):
        for name in os.listdir(parent):
            if name.startswith('onnxruntime-') and name.endswith('.dist-info'):
                version = name[len('onnxruntime-'):-len('.dist-info')]
                break
    return folder, version


def _probe_key(folder, version):
    stamp = ''
    with suppress(OSError):
        stamp = str(int(os.path.getmtime(folder)))
    return '{0}|{1}|{2}|{3}'.format(folder, version, stamp, sys.version_info[:2])


def probe_result(folder, version):
    """True / False if this onnxruntime was probed before, None if not."""
    stored = QgsSettings().value(SETTINGS_PROBE, '', type=str)
    try:
        data = json.loads(stored) if stored else {}
    except ValueError:
        data = {}
    value = data.get(_probe_key(folder, version))
    return None if value is None else bool(value)


def remember_probe(folder, version, ok):
    settings = QgsSettings()
    stored = settings.value(SETTINGS_PROBE, '', type=str)
    try:
        data = json.loads(stored) if stored else {}
    except ValueError:
        data = {}
    data[_probe_key(folder, version)] = bool(ok)
    settings.setValue(SETTINGS_PROBE, json.dumps(data))


def probe_arguments(folder):
    """``python -c ...`` that imports onnxruntime from `folder`'s parent.

    Run in a child process: a crash there costs nothing. The code is a fixed
    string; the folder is passed as an argument, not pasted into it.
    """
    code = ('import sys; sys.path.insert(0, sys.argv[1]); '
            'import numpy, onnxruntime; '
            'onnxruntime.InferenceSession; print("KGA-ORT-OK", '
            'onnxruntime.__version__)')
    return ['-c', code, os.path.dirname(folder)]


def status():
    """Where things stand, without importing anything risky.

    Returns a dict: ``runtime`` is 'ready', 'unprobed', 'broken' or 'missing';
    ``version``, ``folder``; ``model`` is True when the model file is present.
    """
    folder, version = _spec_location()
    if 'onnxruntime' in sys.modules:
        runtime = 'ready'
    elif folder is None:
        runtime = 'missing'
    else:
        known = probe_result(folder, version)
        runtime = 'unprobed' if known is None else (
            'ready' if known else 'broken')
    return {'runtime': runtime, 'version': version, 'folder': folder,
            'model': os.path.isfile(model_path())}


def load_onnxruntime():
    """Import onnxruntime. Call only when status() says 'ready'."""
    _add_libs_path()
    try:
        import onnxruntime
    except Exception as exc:                    # a DLL that will not load
        raise RuntimeProblem(
            'onnxruntime is installed but could not be loaded: {0}'.format(exc))
    return onnxruntime


def check_model_file(path, model=MODEL):
    """Raise RuntimeProblem unless `path` is the model, byte for byte."""
    if not os.path.isfile(path):
        raise RuntimeProblem('The road model has not been downloaded yet.')
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    if digest.hexdigest() != model['sha256']:
        raise RuntimeProblem(
            'The road model file is damaged or not the expected one. Download '
            'it again.')


def open_session(ort, path, threads=0):
    """An inference session on the CPU."""
    options = ort.SessionOptions()
    if threads:
        options.intra_op_num_threads = int(threads)
    options.log_severity_level = 3
    return ort.InferenceSession(path, sess_options=options,
                                providers=['CPUExecutionProvider'])
