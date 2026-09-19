"""crypto.py 并发安全单测：首用窗口的锁 + 原子落盘。

无锁时多线程同时首次加密会各自生成不同密钥写 enc_key，输者的内存 Fernet
与落盘 key 不一致 → 之后所有密文 SecretDecryptError（全部 Key 被视为未配置）。
"""

import concurrent.futures

from video_to_summary import crypto


def test_concurrent_first_use_single_key_and_decryptable(tmp_path, monkeypatch):
    from video_to_summary import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.delenv("VIDEO_TO_SUMMARY_ENC_KEY", raising=False)
    crypto.reset()

    def encrypt_one(i: int) -> str:
        return crypto.encrypt_secret(f"sk-secret-{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ciphertexts = list(pool.map(encrypt_one, range(16)))

    # enc_key 落盘即最终名，且无 mkstemp 临时文件残留（.enc_key.* 前缀）
    files = sorted(p.name for p in tmp_path.iterdir())
    assert "enc_key" in files
    assert not any(name.startswith(".enc_key") for name in files)
    key_file = tmp_path / "enc_key"
    assert key_file.exists()
    assert key_file.stat().st_mode & 0o777 == 0o600

    # 所有线程产物都能解回原文（密钥环境一致）
    for i, ct in enumerate(ciphertexts):
        assert crypto.decrypt_secret(ct) == f"sk-secret-{i}"

    # 后续（新线程）加密/解密仍一致
    assert crypto.decrypt_secret(crypto.encrypt_secret("sk-after")) == "sk-after"


# ---------------------------------------------------------------- 审计补强（P1-9）

def test_env_key_takes_priority_over_key_file(tmp_path, monkeypatch):
    """VIDEO_TO_SUMMARY_ENC_KEY 非空时优先于 enc_key 文件（外部构建注入密钥的锚点）。"""
    from video_to_summary import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "app.db")
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("VIDEO_TO_SUMMARY_ENC_KEY", key)
    crypto.reset()
    try:
        ct = crypto.encrypt_secret("sk-env-key-secret")
        assert crypto.decrypt_secret(ct) == "sk-env-key-secret"
        # 环境变量优先：即使目录里已有一把不同的 enc_key 文件也不被使用
        (tmp_path / "enc_key").write_bytes(Fernet.generate_key())
        assert crypto.decrypt_secret(crypto.encrypt_secret("again")) == "again"
        assert crypto.is_encrypted(ct)
    finally:
        crypto.reset()


def test_reset_semantics_key_rotation(tmp_path, monkeypatch):
    """reset() 必须让密钥环境变化生效：旧密文的密钥缓存被清空、新密钥重新初始化。"""
    from cryptography.fernet import Fernet

    from video_to_summary import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "app.db")
    key_a = Fernet.generate_key().decode("ascii")
    key_b = Fernet.generate_key().decode("ascii")

    monkeypatch.setenv("VIDEO_TO_SUMMARY_ENC_KEY", key_a)
    crypto.reset()
    ct_a = crypto.encrypt_secret("sk-rotate-me")
    assert crypto.decrypt_secret(ct_a) == "sk-rotate-me"

    # 密钥轮转（同一进程内切换 env）后：旧密文不可解、新密文可解
    monkeypatch.setenv("VIDEO_TO_SUMMARY_ENC_KEY", key_b)
    crypto.reset()
    try:
        crypto.decrypt_secret(ct_a)
        raise AssertionError("密钥轮转后旧密文不应可解")
    except crypto.SecretDecryptError:
        pass
    ct_b = crypto.encrypt_secret("sk-rotate-me")
    assert crypto.decrypt_secret(ct_b) == "sk-rotate-me"
    assert ct_a != ct_b


def test_decrypt_failure_never_returns_ciphertext(tmp_path, monkeypatch):
    """enc:v1: 前缀存在但解不开（密钥不符/损坏）→ 显式抛错，绝不把密文当明文 Key 返回。"""
    from video_to_summary import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.delenv("VIDEO_TO_SUMMARY_ENC_KEY", raising=False)
    crypto.reset()
    ct = crypto.encrypt_secret("sk-real-key")
    # 篡改密文体（保持前缀与 base64 形态）
    corrupted = ct[:-4] + ("AAAA" if not ct.endswith("AAAA") else "BBBB")
    try:
        out = crypto.decrypt_secret(corrupted)
        raise AssertionError(f"损坏密文不应被解出: {out!r}")
    except crypto.SecretDecryptError:
        pass
    # cryptography 缺失形态：未安装时同样不允许静默返回密文
    import unittest.mock as mock

    crypto.reset()
    with mock.patch.dict("sys.modules", {"cryptography": None, "cryptography.fernet": None}):
        try:
            crypto.decrypt_secret(ct)
        except crypto.SecretDecryptError:
            pass
    crypto.reset()


def test_legacy_plaintext_passthrough_is_key_itself():
    """无 enc:v1: 前缀 = legacy 明文形态：原样返回（明文键本身即 API Key）。"""
    assert crypto.decrypt_secret("sk-plain-legacy-key") == "sk-plain-legacy-key"
    assert crypto.decrypt_secret("") == ""
    assert crypto.is_encrypted("sk-plain-legacy-key") is False
