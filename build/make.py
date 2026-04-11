"""
To build either run this script or run the following command in the conda env from
within the build folder:

pyinstaller --onefile --name "pdf_player" ../main.py

"""

import PyInstaller.__main__

PyInstaller.__main__.run([
    '../main.py',
    '--onefile',
    '--name=pdf_player',
    '--hidden-import=rocket_fft'
    '--hidden-import=numpy.fft'
])
