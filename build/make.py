"""
Build the single-file executable locally.

Run from anywhere:  python build/make.py

This builds FROM build/pdf_player.spec - the same spec GitHub Actions uses - so local and CI builds match.
It must not regenerate the spec: a generated spec contains absolute paths from this machine, which breaks CI.
Change build options (hidden imports, data files, ...) in pdf_player.spec, not here.

Output: build/dist/pdf_player.exe
"""
from pathlib import Path

import PyInstaller.__main__

REPO_ROOT = Path(__file__).resolve().parent.parent

PyInstaller.__main__.run([
    str(REPO_ROOT / 'build' / 'pdf_player.spec'),
    '--noconfirm',
    '--distpath', str(REPO_ROOT / 'build' / 'dist'),
    '--workpath', str(REPO_ROOT / 'build' / 'build'),
])
