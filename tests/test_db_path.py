"""数据库路径解析测试：resolve_database_path 纯函数 + init_db 目录创建失败提示。

- 纯函数部分：platform / home / environ / package_file 全部显式注入，无需 monkeypatch，
  覆盖 env 覆盖优先、检出布局、安装布局（macOS / Windows / Linux±XDG）。
- init_db 集成部分：父目录缺失自动创建；创建失败（被文件挡住 / 模拟无权限）给出
  含 ``VIDEO_TO_SUMMARY_DB`` 提示的可行动报错。
"""

from pathlib import Path

import pytest

from video_to_summary import db as store_db


def _make_installed_package(tmp_path: Path) -> Path:
    """模拟 site-packages 布局下的 ``__file__``（无 pyproject.toml 祖先）。"""
    db_file = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "video_to_summary" / "db.py"
    db_file.parent.mkdir(parents=True)
    db_file.write_text("", encoding="utf-8")
    return db_file


# ---------------------------------------------------------------- 纯函数解析


def test_env_var_overrides_everything(tmp_path):
    """VIDEO_TO_SUMMARY_DB 设置 → 直接用（检出/安装布局都不再参与，行为不变）。"""
    assert store_db.resolve_database_path(
        env="/custom/db/app.db",
        package_file=tmp_path / "repo" / "src" / "video_to_summary" / "db.py",
        platform="darwin",
        home=Path("/home/user"),
        environ={},
    ) == Path("/custom/db/app.db")


def test_env_var_empty_string_treated_as_unset(tmp_path):
    """空字符串环境变量视为未设置（避免 Path('') = 当前目录的坑），走检出布局。"""
    repo = tmp_path / "repo"
    (repo / "src" / "video_to_summary").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    db_file = repo / "src" / "video_to_summary" / "db.py"
    assert store_db.resolve_database_path(
        env="", package_file=db_file, platform="linux", home=Path("/home/user"), environ={}
    ) == repo / "data" / "app.db"


def test_source_checkout_layout_uses_repo_data_dir(tmp_path):
    """检出布局（parents[2] 含 pyproject.toml + src/video_to_summary/）→ <检出根>/data/app.db。"""
    repo = tmp_path / "repo"
    (repo / "src" / "video_to_summary").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    db_file = repo / "src" / "video_to_summary" / "db.py"
    assert store_db.resolve_database_path(
        env=None, package_file=db_file, platform="linux", home=Path("/home/user"), environ={}
    ) == repo / "data" / "app.db"


def test_installed_layout_macos_user_app_support(tmp_path):
    """安装布局（模拟 site-packages 的 __file__）→ macOS ~/Library/Application Support/VTS/app.db。"""
    db_file = _make_installed_package(tmp_path)
    assert store_db.resolve_database_path(
        env=None, package_file=db_file, platform="darwin", home=Path("/Users/alice"), environ={}
    ) == Path("/Users/alice/Library/Application Support/VTS/app.db")


def test_installed_layout_windows_localappdata(tmp_path):
    """安装布局 + Windows → %LOCALAPPDATA%\\VTS\\app.db；LOCALAPPDATA 未设时回落 AppData\\Local。"""
    db_file = _make_installed_package(tmp_path)
    assert store_db.resolve_database_path(
        env=None, package_file=db_file, platform="win32",
        home=Path("C:/Users/alice"), environ={"LOCALAPPDATA": "C:/Users/alice/AppData/Local"},
    ) == Path("C:/Users/alice/AppData/Local/VTS/app.db")
    assert store_db.resolve_database_path(
        env=None, package_file=db_file, platform="win32",
        home=Path("C:/Users/alice"), environ={},
    ) == Path("C:/Users/alice/AppData/Local/VTS/app.db")


def test_installed_layout_linux_xdg_data_home(tmp_path):
    """安装布局 + Linux + XDG_DATA_HOME → $XDG_DATA_HOME/vts/app.db。"""
    db_file = _make_installed_package(tmp_path)
    assert store_db.resolve_database_path(
        env=None, package_file=db_file, platform="linux",
        home=Path("/home/alice"), environ={"XDG_DATA_HOME": "/home/alice/.local/share"},
    ) == Path("/home/alice/.local/share/vts/app.db")


def test_installed_layout_linux_without_xdg(tmp_path):
    """安装布局 + Linux 无 XDG_DATA_HOME → ~/.local/share/vts/app.db。"""
    db_file = _make_installed_package(tmp_path)
    assert store_db.resolve_database_path(
        env=None, package_file=db_file, platform="linux", home=Path("/home/alice"), environ={}
    ) == Path("/home/alice/.local/share/vts/app.db")


def test_module_default_resolves_to_repo_data_dir():
    """当前测试环境即源码检出：模块导入期 DATABASE_PATH = <检出根>/data/app.db。

    覆盖「导入期可用」契约——现有调用方（crypto._key_path / web 层）依赖它。
    """
    repo = Path(__file__).resolve().parents[1]
    assert store_db.DATABASE_PATH == repo / "data" / "app.db"


# ---------------------------------------------------------------- init_db 集成


def _isolate_db(monkeypatch, tmp_path: Path, db_rel: str = "app.db") -> Path:
    """隔离 DB：指向 tmp_path 下独立路径 + 跳过 legacy JSON 迁移。"""
    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / db_rel)
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "missing_llm_profiles.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "missing_summary_templates.json")
    store_db.reset()
    return tmp_path / db_rel


def test_init_db_creates_missing_parent_dirs(tmp_path, monkeypatch):
    """父目录不存在 → init_db 自动创建（mkdir(parents=True, exist_ok=True)）。"""
    db_file = _isolate_db(monkeypatch, tmp_path, db_rel="nested/data/app.db")
    store_db.init_db()
    assert db_file.exists()
    with store_db.get_conn() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "meta" in tables
    store_db.reset()


def test_init_db_mkdir_failure_mentions_env(tmp_path, monkeypatch):
    """目录创建失败（被同名普通文件挡住）→ 报错信息含 VIDEO_TO_SUMMARY_DB 提示。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    _isolate_db(monkeypatch, tmp_path, db_rel="blocked/app.db")
    with pytest.raises(RuntimeError) as excinfo:
        store_db.init_db()
    message = str(excinfo.value)
    assert "VIDEO_TO_SUMMARY_DB" in message
    assert str(blocker) in message
    store_db.reset()


def test_init_db_permission_error_mentions_env(tmp_path, monkeypatch):
    """模拟不可写目录（Path.mkdir 抛 PermissionError）→ 同样给出可行动提示。"""
    _isolate_db(monkeypatch, tmp_path, db_rel="ro/app.db")

    def deny_mkdir(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", deny_mkdir)
    with pytest.raises(RuntimeError) as excinfo:
        store_db.init_db()
    assert "VIDEO_TO_SUMMARY_DB" in str(excinfo.value)
    store_db.reset()
