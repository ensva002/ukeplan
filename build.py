#!/usr/bin/env python3
"""Build the standalone Ukeplan app on THIS computer and zip it into ./release/.

    python3 build.py        (Windows: py build.py)

Run it once on each operating system you want to ship (PyInstaller can't cross-compile).
Needs Python 3.9+. Everything happens locally in a throw-away virtual environment (.buildenv).
"""
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import venv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".buildenv")
WIN = sys.platform.startswith("win")
MAC = sys.platform == "darwin"


def run(*cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=HERE, check=True)


def write_version():
    """_version.py from the newest git tag (v1.2 -> 1.2). Without git/tags the app reports 'dev'."""
    path = os.path.join(HERE, "_version.py")
    try:
        tag = subprocess.run(["git", "describe", "--tags"], cwd=HERE, capture_output=True, text=True,
                             check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        tag = ""
    if tag:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'VERSION = "{tag.lstrip("v")}"\n')
        print("Version:", tag.lstrip("v"))
    elif os.path.exists(path):
        os.remove(path)


def main():
    os.chdir(HERE)
    write_version()
    if not os.path.isdir(ENV):
        venv.create(ENV, with_pip=True)
    py = os.path.join(ENV, "Scripts" if WIN else "bin", "python" + (".exe" if WIN else ""))
    run(py, "-m", "pip", "install", "-q", "-r", "requirements.txt", "pyinstaller")
    for d in ("build", "dist"):
        shutil.rmtree(os.path.join(HERE, d), ignore_errors=True)
    run(py, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "Ukeplan", "ukeplan.py")

    out = os.path.join(HERE, "release")
    os.makedirs(out, exist_ok=True)
    if WIN:
        name = "Ukeplan-Windows.zip"
        shutil.make_archive(os.path.join(out, name[:-4]), "zip", os.path.join(HERE, "dist"), "Ukeplan")
    elif MAC:
        arm = platform.machine() == "arm64"
        name = "Ukeplan-Mac-AppleSilicon.zip" if arm else "Ukeplan-Mac-Intel.zip"
        app = os.path.join(HERE, "dist", "Ukeplan.app")
        run("codesign", "--force", "--deep", "--sign", "-", app)
        target = os.path.join(out, name)
        if os.path.exists(target):
            os.remove(target)
        run("ditto", "-c", "-k", "--keepParent", app, target)
    else:
        name = "Ukeplan-Linux.tar.gz"
        with tarfile.open(os.path.join(out, name), "w:gz") as t:
            t.add(os.path.join(HERE, "dist", "Ukeplan"), arcname="Ukeplan")
    print("\nDone:", os.path.join(out, name))


if __name__ == "__main__":
    main()
