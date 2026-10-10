#!/usr/bin/env python3
"""Validate wheel/sdist and a clean, non-editable installation, including CLI."""
import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import venv
import zipfile
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    wheel = next(args.dist.glob("*.whl")).resolve()
    source = next(args.dist.glob("*.tar.gz")).resolve()
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))).decode()
        assert f"\nVersion: {args.package_version}\n" in metadata
    with tempfile.TemporaryDirectory(prefix="vts-package-smoke-") as tmp:
        root = Path(tmp)
        with tarfile.open(source) as archive:
            archive.extractall(root / "source", filter="data")
        checkout = next((root / "source").glob("vts-*"))
        rebuilt = root / "rebuilt"
        subprocess.run([sys.executable, "-m", "build", "--wheel", str(checkout), "--outdir", str(rebuilt)], check=True)
        assert next(rebuilt.glob("*.whl")).name == wheel.name
        environment = root / "venv"
        venv.create(environment, with_pip=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", str(wheel)], check=True)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        for key in tuple(env):
            if key.startswith(("VIDEO_TO_SUMMARY_", "VTS_", "SUMMARY_", "ASR_", "LLM_", "OPENAI_", "POLISH_")):
                env.pop(key)
        env.update(VIDEO_TO_SUMMARY_DB=str(root / "app.db"), VIDEO_TO_SUMMARY_OUTPUT_DIR=str(root / "output"))
        cli = subprocess.check_output([str(python), "-m", "video_to_summary.main", "--version"], env=env, cwd=root, text=True)
        assert args.tag in cli
        # Uvicorn gets the bound descriptor rather than probing and reopening a port.
        launcher = root / "serve.py"
        port_file = root / "port"
        launcher.write_text("import socket, pathlib, uvicorn\ns=socket.socket(); s.bind(('127.0.0.1',0))\npathlib.Path('port').write_text(str(s.getsockname()[1]))\nuvicorn.Server(uvicorn.Config('video_to_summary.web.app:app',log_level='warning')).run(sockets=[s])\n")
        server = subprocess.Popen([str(python), str(launcher)], env=env, cwd=root)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    origin = "http://127.0.0.1:" + port_file.read_text()
                    with urlopen(origin + "/api/v1/health", timeout=1) as response:
                        health = json.load(response)
                    break
                except (OSError, ValueError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("Installed wheel readiness failed")
            assert health["version"] == args.tag and health["status"] == "ok"
            with urlopen(origin) as response:
                page = response.read().decode()
            assets = re.findall(r'(?:src|href)="(/static/assets/[^"?]+)"', page)
            assert assets and any(asset.endswith(".js") for asset in assets)
            for asset in assets + ["/guide"]:
                with urlopen(origin + asset) as response:
                    assert response.status == 200 and response.read()
        finally:
            server.terminate()
            server.wait(timeout=10)
    print("wheel + rebuilt sdist + installed CLI/health/static smoke passed")


if __name__ == "__main__":
    main()
