"""持久化单元测试：KV 读写、前缀扫描、发件箱、内存模式降级（跨平台）。"""

import time

import pytest

from arkbot.persist import Persist


@pytest.fixture()
def db(tmp_path):
    p = Persist(tmp_path / "test.db")
    yield p
    p.close()


def test_kv_roundtrip(db):
    db.set("greeting", "你好")
    db.set("count", 42)
    db.set("nested", {"a": [1, 2]})
    assert db.get("greeting") == "你好"
    assert db.get("count") == 42
    assert db.get("nested") == {"a": [1, 2]}


def test_get_default(db):
    assert db.get("missing") is None
    assert db.get("missing", "fallback") == "fallback"


def test_overwrite_and_delete(db):
    db.set("k", 1)
    db.set("k", 2)
    assert db.get("k") == 2
    db.delete("k")
    assert db.get("k") is None
    db.delete("k")  # 重复删除不报错


def test_keys_prefix_scan(db):
    db.set("official.group.1", "g1")
    db.set("official.group.2", "g2")
    db.set("official.user.3", "u3")
    db.set("other", "x")
    keys = db.keys("official.")
    assert len(keys) == 3
    assert all(k.startswith("official.") for k in keys)
    assert db.keys("official.group.") == ["official.group.1", "official.group.2"]


def test_outbox_roundtrip(db):
    items = [
        {"action": "send_group_msg", "params": {"group_id": 1}, "at": time.time()},
        {"action": "send_private_msg", "params": {"user_id": 2}, "at": time.time()},
    ]
    db.save_outbox(items)
    assert db.load_outbox() == items
    db.clear_outbox()
    assert db.load_outbox() == []


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "reopen.db"
    p1 = Persist(path)
    p1.set("durable", "yes")
    p1.close()
    p2 = Persist(path)
    assert p2.get("durable") == "yes"
    p2.close()


def test_fallback_to_memory_mode(tmp_path):
    # 父路径是已存在的文件 → 目录创建必失败（Linux/Windows 通吃）
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("x")
    p = Persist(blocker / "sub" / "x.db")
    assert p._db is None
    assert p.get("anything", "default") == "default"  # 静默返回默认值
    p.set("anything", 1)  # 不抛异常
    assert p.load_outbox() == []
