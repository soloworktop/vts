"""API Key 等敏感配置的 at-rest 加密（可选，基于 cryptography.Fernet 认证加密）。

设计：
- 加密密钥来源：环境变量 ``VIDEO_TO_SUMMARY_ENC_KEY``（Fernet 密钥，base64 文本）；
  未设置时首次使用自动生成并保存到数据库同目录的 ``enc_key`` 文件（chmod 0o600，
  随 ``data/`` 一起被 .gitignore 排除）。
- 未安装 ``cryptography`` 时降级为明文存储并记录 WARNING，保证无依赖环境可用。
- 密文带前缀 ``enc:v1:``；解密失败（密钥变更/数据损坏）时抛 ``SecretDecryptError``，
  由调用方降级（标记未配置 / 视为未登录），避免把密文当明文 Key 使用。
- 密钥独立于数据库存放，即使 DB 被拷贝/误传，密钥不会随之泄露。
"""

import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("video_to_summary.crypto")

_PREFIX = "enc:v1:"
_fernet = None
# 首次并发加密（多任务同时落 Key）会同时走进「生成 enc_key」分支：无锁时各自
# write_bytes 不同密钥，输者内存 Fernet 与落盘 key 不一致 → 之后所有密文解密失败。
_fernet_lock = threading.Lock()


class SecretDecryptError(ValueError):
    """密文前缀存在但无法解密（密钥轮转/损坏/环境不一致）。

    与「legacy 明文」不同：明文键原样即密钥本身，可直接返回；
    带 enc:v1: 前缀却解不开，说明密钥环境已破坏——静默返回密文字符串
    会让上层把密文当明文 Key 使用（作为 Bearer 发往 API、写入日志等），
    因此改为显式抛错，由调用方决定降级（标记未配置 / 视为未登录）。
    """


def _key_path() -> Path:
    # 与数据库同目录存放，测试隔离时随 DB 路径一并隔离
    from . import db

    return Path(db.DATABASE_PATH).parent / "enc_key"


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning("cryptography not installed; API keys stored in plaintext")
        return None

    with _fernet_lock:
        if _fernet is not None:  # 双重检查：等锁期间可能已被其他线程初始化
            return _fernet
        key = os.environ.get("VIDEO_TO_SUMMARY_ENC_KEY")
        if not key:
            key_path = _key_path()
            if not key_path.exists():
                key_path.parent.mkdir(parents=True, exist_ok=True)
                # 原子落盘（mkstemp + os.replace）：绝不出现半写的 enc_key；
                # chmod 在 replace 前对临时文件执行，落盘即 0600
                fd, tmp_name = tempfile.mkstemp(dir=key_path.parent, prefix=".enc_key.")
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(Fernet.generate_key())
                    os.chmod(tmp_name, 0o600)
                    os.replace(tmp_name, key_path)
                except BaseException:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
            key = key_path.read_bytes()
        _fernet = Fernet(key)
        return _fernet


def encrypt_secret(plain: str) -> str:
    if not plain:
        return plain
    fernet = _get_fernet()
    if fernet is None:
        return plain
    return _PREFIX + fernet.encrypt(plain.encode("utf-8")).decode("ascii")


def is_encrypted(stored: str) -> bool:
    """判断库中存储值是否已是本模块的密文形态（enc:v1: 前缀）。"""
    return bool(stored) and stored.startswith(_PREFIX)


def decrypt_secret(stored: str) -> str:
    if not stored or not stored.startswith(_PREFIX):
        return stored  # 未加密（legacy 明文）——原样即密钥本身
    fernet = _get_fernet()
    if fernet is None:
        raise SecretDecryptError("cryptography 不可用，无法解密 enc:v1: 密文")
    try:
        return fernet.decrypt(stored[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - 统一转为 SecretDecryptError
        raise SecretDecryptError("enc:v1: 密文解密失败（密钥轮转/数据损坏？）") from exc


def reset() -> None:
    """测试钩子：清空 Fernet 缓存，便于切换密钥/DB 路径后重新初始化。"""
    global _fernet
    _fernet = None


__all__ = ["encrypt_secret", "decrypt_secret", "reset", "SecretDecryptError"]
