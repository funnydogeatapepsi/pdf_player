# -*- mode: python ; coding: utf-8 -*-
# The one build definition for both CI (pyinstaller build/pdf_player.spec) and local builds (python build/make.py).
# Edit this file by hand; never let PyInstaller regenerate it - a generated spec contains absolute paths from
# the machine that generated it, which breaks the GitHub build.
import os
from PyInstaller.utils.hooks import collect_dynamic_libs

spec_dir = os.path.dirname(os.path.abspath(SPEC))
repo_root = os.path.dirname(spec_dir)

a = Analysis(
    [os.path.join(repo_root, 'main.py')],
    pathex=[repo_root],
    # rocket_fft locates its compiled helpers by globbing the filesystem and loads them with ctypes/llvmlite,
    # so PyInstaller's import analysis can't see them. Without these, np.fft inside @njit (the phase vocoder)
    # fails to compile in the exe.
    binaries=collect_dynamic_libs('rocket_fft', search_patterns=['_*.pyd', '_*.so']),
    datas=[],
    hiddenimports=['rocket_fft', 'numpy.fft'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='pdf_player',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
