"""Build a standalone StashSorter.exe (no Python needed to run it).

    python tools/build_exe.py

Creates an isolated build environment in .venv-build/, installs PyInstaller there (nothing is installed into your
normal Python), and writes dist/StashSorter.exe.
"""

import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv-build"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(*args):
    print("+", " ".join(str(a) for a in args))
    subprocess.run([str(a) for a in args], check=True, cwd=ROOT)


def main():
    if not PY.exists():
        venv.create(VENV, with_pip=True)
    run(PY, "-m", "pip", "install", "--quiet", "--upgrade", "pyinstaller")
    sep = ";" if os.name == "nt" else ":"
    data = [("stash_sorter/web", "stash_sorter/web"), ("stash_sorter/data", "stash_sorter/data"),
            ("stash_sorter/default_rules.json", "stash_sorter")]
    args = [PY, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console", "--name", "StashSorter",
            "--distpath", ROOT / "dist", "--workpath", VENV / "work", "--specpath", VENV]
    for src, dst in data:
        args += ["--add-data", f"{ROOT / src}{sep}{dst}"]
    args.append(ROOT / "tools" / "exe_entry.py")
    run(*args)
    print(f"\nBuilt {ROOT / 'dist' / 'StashSorter.exe'}")


if __name__ == "__main__":
    sys.exit(main())
