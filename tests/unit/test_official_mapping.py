"""官方通道映射单元测试：伪 ID 稳定性、学习、持久化挂载与恢复。"""

import pytest

from arkbot.adapters.official_qq import OfficialQQAdapter, _pseudo_id
from arkbot.persist import Persist


def test_pseudo_id_stable_and_positive():
    a = _pseudo_id("GROUP_OPENID_ABC")
    b = _pseudo_id("GROUP_OPENID_ABC")
    c = _pseudo_id("OTHER")
    assert a == b
    assert a != c
    assert 0 < a < 2**31


def test_learn_maps_openid_to_pseudo_id():
    ob = OfficialQQAdapter(app_id="a", app_secret="b")
    pid = ob._learn_group("G1")
    assert ob._group_openid_by_id[pid] == "G1"
    uid = ob._learn_user("U1")
    assert ob._user_openid_by_id[uid] == "U1"
    # 重复学习不覆盖、不新增
    assert ob._learn_group("G1") == pid
    assert len(ob._group_openid_by_id) == 1


def test_learn_empty_openid_ignored():
    ob = OfficialQQAdapter(app_id="a", app_secret="b")
    pid = ob._learn_group("")
    assert pid in ob._group_openid_by_id is False or ob._group_openid_by_id == {}


def test_attach_persist_restores_maps(tmp_path):
    # 进程 1：学习并落盘
    p1 = Persist(tmp_path / "map.db")
    ob1 = OfficialQQAdapter(app_id="a", app_secret="b")
    ob1.attach_persist(p1)
    g = ob1._learn_group("GROUP_X")
    u = ob1._learn_user("USER_Y")
    p1.close()

    # 进程 2：全新实例恢复
    p2 = Persist(tmp_path / "map.db")
    ob2 = OfficialQQAdapter(app_id="a", app_secret="b")
    ob2.attach_persist(p2)
    assert ob2._group_openid_by_id.get(g) == "GROUP_X"
    assert ob2._user_openid_by_id.get(u) == "USER_Y"
    # 恢复后的映射可直接用于主动发送反查（不抛 KeyError）
    assert ob2._group_openid_by_id[g] == "GROUP_X"
    p2.close()


def test_attach_persist_none_is_noop():
    ob = OfficialQQAdapter(app_id="a", app_secret="b")
    ob.attach_persist(None)  # 不应抛异常
    ob._learn_group("G")
    assert len(ob._group_openid_by_id) == 1
