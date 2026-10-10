#!/usr/bin/env python3
"""Bootstrap a hashed build backend, then install without isolated unpinned builds."""
import argparse
import re
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", choices=("macos", "windows"))
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Desktop builds require CPython 3.12")
    root = Path(__file__).resolve().parents[1]
    lock = root / f"packaging/desktop/requirements-{args.platform}.lock"
    sections = re.split(r"(?=^[A-Za-z0-9_-]+==)", lock.read_text(), flags=re.M)
    bootstrap = [section for section in sections if re.match(r"(?:setuptools|wheel|packaging)==", section)]
    destination = root / "build/desktop/bootstrap.lock"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(bootstrap))
    subprocess.run([sys.executable, "-m", "pip", "install", "--require-hashes", "-r", str(destination)], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--require-hashes", "--no-build-isolation", "-r", str(lock)], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", str(root)], check=True)


if __name__ == "__main__":
    main()
