"""
Build a single-file executable with PyInstaller.

Either run this script, or from the repo root run:

    pyinstaller build/pdf_player.spec

The GitHub Actions workflow (.github/workflows/build.yml) does the same thing on every tag push.
"""
from pathlib import Path

import PyInstaller.__main__

REPO_ROOT = Path(__file__).resolve().parent.parent

PyInstaller.__main__.run([
    str(REPO_ROOT / 'main.py'),
    '--onefile',
    '--name=pdf_player',
    '--hidden-import=numpy.fft',
    '--distpath', str(REPO_ROOT / 'build' / 'dist'),
    '--workpath', str(REPO_ROOT / 'build' / 'build'),
    '--specpath', str(REPO_ROOT / 'build'),
])
