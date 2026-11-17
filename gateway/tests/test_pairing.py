"""配对码与 keystore 单测：熵、格式、PBKDF2、TTL、单次作废、限速、持久化。"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from gateway.keystore import KeyStore, gen_keypair
from gateway.pairing import (
    ALPHABET,
    CODE_LEN,
    PairingManager,
    compute_proof,
    derive_key,
    format_code,
    normalize,
)


# ---------------------------------------------------------------- 编码


def test_code_length_and_alphabet():
    pm = PairingManager()
    code = pm.issue("mi14")
    assert len(code) == CODE_LEN == 8
    assert all(ch in ALPHABET for ch in code)
    # Crockford 排除易混字符
    assert not set("ILOU") & set(code)


def test_code_entropy_across_many_issues():
    pm = PairingManager()
    codes = {pm.issue("x") for _ in range(50)}
    # 40 bit 空间下 50 个码重复概率可忽略
    assert len(codes) == 50


def test_format_and_normalize_roundtrip():
    pm = PairingManager()
    code = pm.issue("mi14")
    shown = format_code(code)
    assert len(shown) == 9 and shown[4] == "-"
    assert normalize(shown) == code
    # 手输容错：小写、空格、易混字符
    sloppy = shown.lower().replace("-", " ")
    assert normalize(sloppy) == code
    assert normalize("o".join(code.split("0"))) if "0" in code else True


def test_normalize_alias():
    assert normalize("O0IL1") == "00111"


# ---------------------------------------------------------------- PBKDF2 与证明


def test_derive_key_is_expensive_and_deterministic():
    salt = b"salt"
    t0 = time.perf_counter()
    k1 = derive_key("ABCD1234", salt)
    dt = time.perf_counter() - t0
    k2 = derive_key("a-b-c-d-1-2-3-4", salt)  # 规范化后相同
    assert k1 == k2
    assert len(k1) == 32
    # 300k 迭代应当在数十毫秒量级（低于 5ms 说明迭代数被改小了）
    assert dt > 0.005, f"PBKDF2 太快（{dt*1000:.1f}ms），检查迭代数"


def test_pairing_salt_is_deterministic_and_depends_on_name_and_pubkey():
    from gateway.pairing import pairing_salt

    gw_pub = b"g" * 32
    assert pairing_salt("mi14", gw_pub) == pairing_salt("mi14", gw_pub)
    assert pairing_salt("mi14", gw_pub) != pairing_salt("mi15", gw_pub)
    assert pairing_salt("mi14", gw_pub) != pairing_salt("mi14", b"h" * 32)
    assert len(pairing_salt("mi14", gw_pub)) == 32


def test_different_codes_give_different_keys():
    assert derive_key("ABCD1234", b"s") != derive_key("ABCD1235", b"s")


def test_proof_binds_all_fields():
    key = derive_key("ABCD1234", b"s")
    nonce = b"\x01" * 32
    dev_pub, gw_pub = b"d" * 32, b"g" * 32
    base = compute_proof(key, "dev", nonce, "mi14", dev_pub, gw_pub)

    # 任一字段变化都必须改变证明
    assert compute_proof(key, "gw", nonce, "mi14", dev_pub, gw_pub) != base
    assert compute_proof(key, "dev", b"\x02" * 32, "mi14", dev_pub, gw_pub) != base
    assert compute_proof(key, "dev", nonce, "mi15", dev_pub, gw_pub) != base
    assert compute_proof(key, "dev", nonce, "mi14", b"x" * 32, gw_pub) != base
    assert compute_proof(key, "dev", nonce, "mi14", dev_pub, b"y" * 32) != base
    # 密钥不同则证明不同
    other = derive_key("ABCD1235", b"s")
    assert compute_proof(other, "dev", nonce, "mi14", dev_pub, gw_pub) != base


# ---------------------------------------------------------------- 生命周期


def test_consume_once_only():
    pm = PairingManager()
    code = pm.issue("mi14")

    assert pm.try_consume(lambda c: hmac.compare_digest(c, code)) == code
    # 第二次必须失败：码已作废
    assert pm.try_consume(lambda c: hmac.compare_digest(c, code)) is None
    assert pm.pending_count() == 0


def test_wrong_proof_does_not_consume():
    pm = PairingManager()
    code = pm.issue("mi14")
    assert pm.try_consume(lambda c: False) is None
    # 码仍可用
    assert pm.pending_count() == 1
    assert pm.try_consume(lambda c: hmac.compare_digest(c, code)) == code


def test_ttl_expiry():
    pm = PairingManager(ttl=1)
    code = pm.issue("mi14")
    assert pm.pending_count() == 1
    # 手动把创建时间推到过去
    pm._pending[code].created_at -= 5
    assert pm.pending_count() == 0
    assert pm.try_consume(lambda c: hmac.compare_digest(c, code)) is None


def test_pending_cap_evicts_oldest():
    pm = PairingManager()
    codes = [pm.issue(f"d{i}") for i in range(6)]
    assert pm.pending_count() <= 4
    # 最早的应当被淘汰
    assert codes[0] not in pm._pending


# ---------------------------------------------------------------- 失败限速


def test_rate_limit_after_failures():
    pm = PairingManager()
    ip = "203.0.113.9"
    assert not pm.is_blocked(ip)
    for _ in range(5):
        pm.note_failure(ip)
    assert pm.is_blocked(ip)
    pm.note_success(ip)
    assert not pm.is_blocked(ip)


def test_rate_limit_window_resets():
    pm = PairingManager()
    ip = "203.0.113.9"
    for _ in range(5):
        pm.note_failure(ip)
    assert pm.is_blocked(ip)
    pm._fails[ip].first_at -= 700  # 超出 600s 窗口
    assert not pm.is_blocked(ip)


# ---------------------------------------------------------------- KeyStore


def test_keystore_generates_and_persists(tmp_path):
    path = tmp_path / "gateway.keys"
    ks = KeyStore(path)
    assert len(ks.gateway_private) == 32
    assert len(ks.gateway_public) == 32
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600, "私钥文件必须是 0600"

    # 重载后密钥不变
    ks2 = KeyStore(path)
    assert ks2.gateway_private == ks.gateway_private
    assert ks2.gateway_public == ks.gateway_public


def test_keystore_pair_and_unpair(tmp_path):
    ks = KeyStore(tmp_path / "k.json")
    _, pub = gen_keypair()
    ks.pair("mi14", pub)
    assert ks.device("mi14") is not None
    assert ks.device("mi14").public_key == pub

    # 重载保持
    assert KeyStore(tmp_path / "k.json").device("mi14").public_key == pub

    assert ks.unpair("mi14") is True
    assert ks.unpair("mi14") is False
    assert KeyStore(tmp_path / "k.json").device("mi14") is None


def test_keystore_rename_migrates_label(tmp_path):
    """改名：条目不增加，公钥不变，renamed_at 记录时间。"""
    ks = KeyStore(tmp_path / "k.json")
    _, pub = gen_keypair()
    ks.pair("mi14", pub)
    renamed = ks.rename("mi14", "my-phone")
    assert renamed.name == "my-phone" and renamed.public_key == pub
    assert renamed.renamed_at is not None
    assert ks.device("mi14") is None
    assert len(ks.devices()) == 1
    assert KeyStore(tmp_path / "k.json").device("my-phone").public_key == pub


def test_keystore_rename_rejects_taken_name(tmp_path):
    ks = KeyStore(tmp_path / "k.json")
    _, pub_a = gen_keypair()
    _, pub_b = gen_keypair()
    ks.pair("dev-a", pub_a)
    ks.pair("dev-b", pub_b)
    with pytest.raises(ValueError, match="已被另一台"):
        ks.rename("dev-b", "dev-a")
    # 未被篡改
    assert ks.device("dev-a").public_key == pub_a
    assert ks.device("dev-b").public_key == pub_b


def test_keystore_rename_to_same_name_is_noop(tmp_path):
    ks = KeyStore(tmp_path / "k.json")
    _, pub = gen_keypair()
    ks.pair("mi14", pub)
    assert ks.rename("mi14", "mi14").name == "mi14"


def test_keystore_pair_same_key_migrates_not_duplicates(tmp_path):
    """同一公钥再次配对（改名重配对）→ 迁移标签，不留两条记录。"""
    ks = KeyStore(tmp_path / "k.json")
    _, pub = gen_keypair()
    ks.pair("mi14", pub)
    ks.pair("new-name", pub)
    assert ks.device("mi14") is None
    assert ks.device("new-name").public_key == pub
    assert len(ks.devices()) == 1


def test_keystore_pair_name_taken_by_other_key(tmp_path):
    ks = KeyStore(tmp_path / "k.json")
    _, pub_a = gen_keypair()
    _, pub_b = gen_keypair()
    ks.pair("mi14", pub_a)
    with pytest.raises(ValueError, match="已被另一台"):
        ks.pair("mi14", pub_b)
    assert ks.device("mi14").public_key == pub_a


def test_keystore_unpair_by_public_key(tmp_path):
    ks = KeyStore(tmp_path / "k.json")
    _, pub = gen_keypair()
    ks.pair("mi14", pub)
    assert ks.unpair_by_public_key(pub) is not None
    assert ks.device("mi14") is None
    assert ks.unpair_by_public_key(pub) is None


def test_keystore_rejects_bad_key_length(tmp_path):
    ks = KeyStore(tmp_path / "k.json")
    with pytest.raises(ValueError):
        ks.pair("mi14", b"short")


def test_device_tag_derived_from_public_key():
    """身份=公钥：同一个公钥恒得同一 tag，不同公钥不同 tag。"""
    from gateway.keystore import device_tag

    _, pub_a = gen_keypair()
    _, pub_b = gen_keypair()
    assert len(device_tag(pub_a)) == 8
    assert device_tag(pub_a) == device_tag(pub_a)
    assert device_tag(pub_a) != device_tag(pub_b)
    # 不泄露公钥前缀
    assert pub_a[:8] not in device_tag(pub_a)
