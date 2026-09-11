"""Build kga_tools.zip, the archive QGIS installs via Plugins > Install from ZIP.

Run from this folder:  python package.py

The guard below is not decoration. QGIS picks the plugin name out of the zip by
collecting every entry whose name ends in "metadata.txt", sorting those
*alphabetically* and splitting the first one (pyplugin_installer/installer.py,
installFromZipFile). It assumes the alphabetically-first path is the shortest
one. A file such as algorithms/dem_contour_tool_qgis_metadata.txt therefore
wins over the real kga_tools/metadata.txt, and QGIS extracts the plugin
correctly and then fails with:

    ModuleNotFoundError: No module named 'kga_tools/algorithms'

So: exactly one entry in the archive may end in "metadata.txt", and it has to be
the plugin's own. Name per-algorithm notes "..._notes.txt", never "..._metadata.txt".
"""

import os
import sys
import zipfile

SRC = 'kga_tools'
OUT = 'kga_tools.zip'
SKIP_DIRS = {'__pycache__', '.git'}
SKIP_EXT = {'.pyc', '.pyo'}


def collect():
    for root, dirs, names in os.walk(SRC):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            if os.path.splitext(name)[1] in SKIP_EXT:
                continue
            yield os.path.join(root, name).replace(os.sep, '/')


def main():
    files = list(collect())

    expected = SRC + '/metadata.txt'
    stray = [f for f in files if f.endswith('metadata.txt') and f != expected]
    if stray:
        sys.exit(
            'Refusing to build: these would hijack the plugin name QGIS reads '
            'from the archive.\n  ' + '\n  '.join(stray) +
            '\nRename them so they do not end in "metadata.txt".')
    if expected not in files:
        sys.exit('Refusing to build: {} is missing.'.format(expected))

    with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path)

    version = next((line.split('=', 1)[1].strip()
                    for line in open(os.path.join(SRC, 'metadata.txt'),
                                     encoding='utf-8')
                    if line.startswith('version=')), '?')
    print('{}: {} files, {} KB, version {}'.format(
        OUT, len(files), os.path.getsize(OUT) // 1024, version))


if __name__ == '__main__':
    main()
