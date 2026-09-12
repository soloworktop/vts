#!/usr/bin/env python3
"""重新生成 OpenAPI 契约快照 tests/openapi_paths.json（防接口漂移测试的快照）。

在仓库根执行：``python scripts/snapshot_openapi.py``
等价于：``VTS_UPDATE_OPENAPI_SNAPSHOT=1 python -m pytest tests/test_openapi_contract.py``

用途：接口有意的增删改后，用本脚本刷新快照并提交；无意的接口漂移会在
``tests/test_openapi_contract.py`` 被拦下。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

OUT = REPO_ROOT / "tests" / "openapi_paths.json"


def normalize(path: str) -> str:
    """路径参数归一：``{job_id}`` 与 ``{label_id}`` 等视为同一路径 ``{}``。"""
    return re.sub(r"\{[^}]+\}", "{}", path)


def main() -> None:
    from video_to_summary.web.app import app

    paths = sorted(normalize(p) for p in app.openapi().get("paths", {}))
    OUT.write_text(json.dumps(paths, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[snapshot] wrote {len(paths)} paths -> {OUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
