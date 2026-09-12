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
