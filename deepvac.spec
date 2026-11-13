# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for Deepvac Insight."""

from PyInstaller.building.build_main import Tree

resources_tree = Tree(
    "resources",
    prefix="resources",
    excludes=["*.ts", "_translate.py", "__pycache__"],
)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("app/model/model.pt", "app/model")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebChannel",
        "PySide6.QtWebSockets",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtPdf",
    ],
    noarchive=False,
)

a.datas += resources_tree
a.datas += Tree("wiki", prefix="wiki", excludes=["*.tmp"])

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Deepvac",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="resources/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Deepvac",
)
