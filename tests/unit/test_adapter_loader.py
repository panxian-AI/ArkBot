"""适配器加载器测试：内置 / 点分路径 / entry point / 错误提示。"""

from __future__ import annotations

import sys
import types

import pytest

from arkbot.adapters.loader import available, load_adapter, register
from arkbot.adapters.onebot11 import OneBot11Adapter


class DummyAdapter:
    name = "dummy"

    def __init__(self, base_url: str = "", access_token: str = "", **kwargs):
        self.base_url = base_url
        self.access_token = access_token
        self.extra = kwargs

    async def call_api(self, action, params=None):
        return {}


@pytest.fixture()
def dummy_module(tmp_path, monkeypatch):
    module = types.ModuleType("arkbot_dummy_plugin")
    module.DummyAdapter = DummyAdapter
    module.create = lambda **cfg: DummyAdapter(**cfg)
    monkeypatch.setitem(sys.modules, "arkbot_dummy_plugin", module)
    return module


def test_builtin_types_present():
    names = available()
    assert "onebot11" in names and "official_qq" in names


def test_builtin_onebot_constructed_from_entry():
    adapter = load_adapter({"type": "onebot11", "url": "ws://127.0.0.1:6700", "access_token": "t"})
    assert isinstance(adapter, OneBot11Adapter)
    assert adapter.url == "ws://127.0.0.1:6700"
    assert adapter.access_token == "t"


def test_dotted_path(dummy_module):
    adapter = load_adapter({
        "type": "arkbot_dummy_plugin:DummyAdapter",
        "base_url": "http://x",
        "access_token": "tok",
    })
    assert isinstance(adapter, DummyAdapter)
    assert adapter.base_url == "http://x" and adapter.access_token == "tok"


def test_dotted_path_factory(dummy_module):
    adapter = load_adapter({"type": "arkbot_dummy_plugin:create", "base_url": "http://y"})
    assert adapter.base_url == "http://y"


def test_entry_point_discovery(monkeypatch, dummy_module):
    class FakeEP:
        name = "third-party"
        value = "arkbot_dummy_plugin:create"

        def load(self):
            return dummy_module.create

    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda group=None: [FakeEP()])
    adapter = load_adapter({"type": "third-party", "base_url": "http://ep"})
    assert adapter.base_url == "http://ep"


def test_unknown_type_message_points_to_entry_points():
    with pytest.raises(ValueError) as ei:
        load_adapter({"type": "no-such-adapter"})
    msg = str(ei.value)
    assert "entry point" in msg and "onebot11" in msg


def test_bad_module_path():
    with pytest.raises(ModuleNotFoundError):
        load_adapter({"type": "nope_module:Adapter"})


def test_missing_attr(dummy_module):
    with pytest.raises(ImportError):
        load_adapter({"type": "arkbot_dummy_plugin:Missing"})


def test_wrong_kwargs_reports_fields():
    with pytest.raises(TypeError) as ei:
        load_adapter({"type": "onebot11", "url": "ws://x", "unknown_field": 1})
    assert "unknown_field" in str(ei.value)


def test_object_not_satisfying_contract():
    module = types.ModuleType("arkbot_bad_plugin")
    module.create = lambda **cfg: object()
    sys.modules["arkbot_bad_plugin"] = module
    try:
        with pytest.raises(TypeError) as ei:
            load_adapter({"type": "arkbot_bad_plugin:create"})
        assert "契约" in str(ei.value)
    finally:
        sys.modules.pop("arkbot_bad_plugin", None)


def test_register_custom_type():
    register("my-inproc", lambda **cfg: DummyAdapter(**cfg))
    adapter = load_adapter({"type": "my-inproc", "base_url": "http://z"})
    assert adapter.base_url == "http://z"
