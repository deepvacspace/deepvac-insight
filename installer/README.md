# Building a distributable installer

Two steps: freeze the app with PyInstaller, then wrap that into a real
Windows installer with Inno Setup.

## 1. Freeze the app

```
uv sync --extra dev
uv run pyinstaller deepvac.spec --clean
```

Output goes to `dist/Deepvac/Deepvac.exe`.

## 2. Wrap it in an installer

Requires [Inno Setup](https://jrsoftware.org/isinfo.php) (`winget install
JRSoftware.InnoSetup`).

```
iscc installer\deepvac.iss
```

Output: `installer\output\DeepvacInsight-Setup-<version>.exe` -- a normal
Windows installer with a wizard, a Start Menu entry, an optional desktop
shortcut, and a standard uninstaller (Add/Remove Programs).

## Where things live once installed

- The app itself: wherever the installer put it.
- User data (the sqlite databases, logs, backups, generated reports):
  `%LOCALAPPDATA%\DeepVac\data`, created by the app on first run.

## Known limitations

- Not code-signed. Windows SmartScreen will show an "unknown publisher"
  warning on first run of the installer until/unless this is signed with
  a real code-signing certificate -- there isn't one configured here.
- Version number is hand-set in three places that need to stay in sync:
  `pyproject.toml`, `installer/deepvac.iss` (`MyAppVersion`).
- No auto-update mechanism -- a new version means re-running both steps
  above and distributing the new installer manually.
