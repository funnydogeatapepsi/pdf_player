from pathlib import Path
import subprocess

parent_path = Path(__file__).parent.parent
design_path = parent_path / "design"
window_dir = parent_path / "windows"
window_dir.mkdir(parents=True, exist_ok=True)

ui_files = [path for path in design_path.glob('*.ui')]

for ui_file in ui_files:
    command = ["pyside6-uic", "-g", "python", "-o", f'{str(window_dir / ui_file.stem)}.py', str(ui_file)]
    print(' '.join(command))
    subprocess.run(command)


