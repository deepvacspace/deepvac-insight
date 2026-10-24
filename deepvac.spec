# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for DeepVac Insight.

Build (onedir -- torch is large enough that --onefile's per-launch
re-extraction into a temp dir would make every startup slow; onedir starts
instantly after the first run):

    pyinstaller deepvac.spec

Output: dist/DeepVac/DeepVac.exe, plus its bundled resources/ and
app/model/model.pt sitting alongside it, plus every Python/Qt/torch
dependency. Feed that dist/DeepVac/ folder to installer/deepvac.iss (Inno
Setup) to produce a real Windows installer with Start Menu shortcuts and an
uninstaller -- see installer/README.md.

User-writable data (databases, logs, backups, generated reports) is never
bundled here: app/common.py resolves it to %LOCALAPPDATA%\\DeepVac\\data at
runtime when frozen (sys.frozen), independent of wherever this bundle
itself ends up installed (e.g. Program Files, which a normal user account
can't write to).
"""
from PyInstaller.building.build_main import Tree

# resources/i18n/ also holds the .ts translation *sources* and the
# hand-maintained _translate.py script used to (re)fill them -- dev-only
# tooling, not needed at runtime (only the compiled .qm files are loaded).
resources_tree = Tree(
    'resources',
    prefix='resources',
    excludes=['*.ts', '_translate.py', '__pycache__'],
)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('app/model/model.pt', 'app/model')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

a.datas += resources_tree
# the crew's wiki: seeds DATA_DIR/wiki on first run (app/services/wiki_service.py)
a.datas += Tree('wiki', prefix='wiki', excludes=['*.tmp'])

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DeepVac',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='resources/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='DeepVac',
)
