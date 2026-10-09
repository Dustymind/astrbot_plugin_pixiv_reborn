"""
代理解析的一致性测试。

背景：pixivpy3 底层用 `requests`，而 `requests` **默认就会读** `HTTPS_PROXY` / `HTTP_PROXY`
等环境变量，所以"只配了系统代理、插件里留空"的环境下 API 调用本来就能通。
但 aiohttp 不会自动读环境变量，导致同一份配置下：
    - 搜图正常（requests 走了环境变量代理）
    - 珍藏册抓取 / 图片下载直连超时（aiohttp 没走）
本文件锁住修复后的行为：三处判断统一收敛到 `PixivConfig.effective_proxy`。
"""

import os
import sys
import types
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

try:
    import astrbot.api  # noqa: F401
except ImportError:
    _astrbot = ModuleType("astrbot")
    _api = ModuleType("astrbot.api")
    _api.logger = SimpleNamespace(
        debug=Mock(), info=Mock(), warning=Mock(), error=Mock()
    )
    _event = ModuleType("astrbot.api.event")
    _event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    _components = ModuleType("astrbot.api.message_components")
    for _name in ("Image", "Plain", "Node", "Nodes"):
        setattr(_components, _name, type(_name, (), {}))
    _api.event = _event
    _api.message_components = _components
    _astrbot.api = _api
    sys.modules.update(
        {
            "astrbot": _astrbot,
            "astrbot.api": _api,
            "astrbot.api.event": _event,
            "astrbot.api.message_components": _components,
        }
    )

_PKG_NAME = "pixiv_plugin_under_test"
if _PKG_NAME not in sys.modules:
    _pkg = types.ModuleType(_PKG_NAME)
    _pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[_PKG_NAME] = _pkg

from pixiv_plugin_under_test.core.client import PixivClientWrapper  # noqa: E402
from pixiv_plugin_under_test.utils.config import PixivConfig  # noqa: E402
from pixiv_plugin_under_test.utils import pixiv_utils as pu  # noqa: E402

PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def _env_without_proxy(**extra: str) -> dict:
    """构造一个不含任何代理变量的干净环境，避免宿主环境影响断言。"""
    env = {k: v for k, v in os.environ.items() if k not in PROXY_ENV_KEYS}
    env.update(extra)
    return env


def _clean_env(**proxy_vars: str):
    return patch.dict(os.environ, _env_without_proxy(**proxy_vars), clear=True)


def _make_config(**overrides) -> PixivConfig:
    data = {"refresh_token": "fake-token"}
    data.update(overrides)
    return PixivConfig(data)


class EffectiveProxyTests(unittest.TestCase):
    def test_config_proxy_wins_over_environment(self):
        with _clean_env(HTTPS_PROXY="http://env.invalid:8080"):
            config = _make_config(proxy="http://config.invalid:7890")
            self.assertEqual(config.effective_proxy, "http://config.invalid:7890")

    def test_falls_back_to_https_proxy(self):
        with _clean_env(HTTPS_PROXY="http://env.invalid:8080"):
            config = _make_config(proxy="")
            self.assertEqual(config.effective_proxy, "http://env.invalid:8080")

    def test_falls_back_to_lowercase_env(self):
        with _clean_env(https_proxy="http://lower.invalid:8080"):
            config = _make_config(proxy="")
            self.assertEqual(config.effective_proxy, "http://lower.invalid:8080")

    def test_http_proxy_env_is_honoured(self):
        with _clean_env(HTTP_PROXY="http://only-http.invalid:8080"):
            config = _make_config(proxy="")
            self.assertEqual(config.effective_proxy, "http://only-http.invalid:8080")

    def test_empty_when_nothing_configured(self):
        with _clean_env():
            config = _make_config(proxy="")
            self.assertEqual(config.effective_proxy, "")
            self.assertEqual(config.get_requests_kwargs(), {})

    def test_blank_env_values_are_ignored(self):
        with _clean_env(HTTPS_PROXY="   ", HTTP_PROXY=""):
            config = _make_config(proxy="")
            self.assertEqual(config.effective_proxy, "")
            self.assertEqual(config.get_requests_kwargs(), {})

    def test_get_requests_kwargs_uses_effective_proxy(self):
        with _clean_env(HTTPS_PROXY="http://env.invalid:8080"):
            config = _make_config(proxy="")
            self.assertEqual(
                config.get_requests_kwargs(),
                {
                    "proxies": {
                        "http": "http://env.invalid:8080",
                        "https": "http://env.invalid:8080",
                    }
                },
            )

    def test_get_requests_kwargs_prefers_config_proxy(self):
        with _clean_env(HTTPS_PROXY="http://env.invalid:8080"):
            config = _make_config(proxy="http://config.invalid:7890")
            self.assertEqual(
                config.get_requests_kwargs(),
                {
                    "proxies": {
                        "http": "http://config.invalid:7890",
                        "https": "http://config.invalid:7890",
                    }
                },
            )


class EffectiveProxyHelperTests(unittest.TestCase):
    """pixiv_utils._effective_proxy 需要兼容只有 proxy 属性的替身配置。"""

    def test_prefers_effective_proxy_attribute(self):
        config = SimpleNamespace(
            effective_proxy="http://eff.invalid:1", proxy="http://raw:2"
        )
        self.assertEqual(pu._effective_proxy(config), "http://eff.invalid:1")

    def test_falls_back_to_plain_proxy_attribute(self):
        config = SimpleNamespace(proxy="http://raw.invalid:2")
        self.assertEqual(pu._effective_proxy(config), "http://raw.invalid:2")

    def test_trims_whitespace(self):
        config = SimpleNamespace(proxy="  http://raw.invalid:2  ")
        self.assertEqual(pu._effective_proxy(config), "http://raw.invalid:2")

    def test_missing_attributes_yield_empty(self):
        self.assertEqual(pu._effective_proxy(SimpleNamespace()), "")


class _FakeResponse:
    status = 200

    async def read(self) -> bytes:
        return b"image-bytes"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, proxy=None, timeout=None):
        self.calls.append({"url": url, "proxy": proxy})
        return _FakeResponse()


IMG_URL = (
    "https://i.pximg.net/img-master/img/2024/09/07/09/12/38/122208118_p0_master1200.jpg"
)


class DownloadImageProxyTests(unittest.IsolatedAsyncioTestCase):
    """只读环境变量代理是不够的：图片下载必须真的把代理传给 aiohttp。"""

    async def test_effective_proxy_is_passed_and_reverse_proxy_skipped(self):
        config = SimpleNamespace(
            proxy="", effective_proxy="http://127.0.0.1:7890", use_image_proxy=True
        )
        session = _FakeSession()

        with patch.object(pu, "_config", config):
            data = await pu.download_image(session, IMG_URL)

        self.assertEqual(data, b"image-bytes")
        self.assertEqual(session.calls[0]["proxy"], "http://127.0.0.1:7890")
        # 有可用代理时不应改写为 i.pixiv.re 反代
        self.assertIn("i.pximg.net", session.calls[0]["url"])
        self.assertNotIn("i.pixiv.re", session.calls[0]["url"])

    async def test_without_proxy_falls_back_to_reverse_proxy(self):
        config = SimpleNamespace(proxy="", effective_proxy="", use_image_proxy=True)
        session = _FakeSession()

        with patch.object(pu, "_config", config):
            await pu.download_image(session, IMG_URL)

        self.assertIsNone(session.calls[0]["proxy"])
        self.assertIn("i.pixiv.re", session.calls[0]["url"])

    async def test_reverse_proxy_can_be_disabled_explicitly(self):
        config = SimpleNamespace(proxy="", effective_proxy="", use_image_proxy=False)
        session = _FakeSession()

        with patch.object(pu, "_config", config):
            await pu.download_image(session, IMG_URL)

        self.assertIsNone(session.calls[0]["proxy"])
        self.assertIn("i.pximg.net", session.calls[0]["url"])


class ClientWrapperProxyBranchTests(unittest.TestCase):
    """只配了环境变量代理时，也应走代理分支而不是直连/ByPassSni。"""

    def _config(self, **overrides):
        base = dict(
            proxy="",
            api_proxy_host="",
            refresh_token="fake-token",
            refresh_interval=180,
            effective_proxy="",
            get_requests_kwargs=lambda: {},
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_env_proxy_selects_app_api_proxy_branch(self):
        config = self._config(
            effective_proxy="http://env.invalid:8080",
            get_requests_kwargs=lambda: {
                "proxies": {"http": "http://env.invalid:8080"}
            },
        )
        with patch("pixiv_plugin_under_test.core.client.AppPixivAPI") as api:
            PixivClientWrapper(config)
            api.assert_called_once_with(proxies={"http": "http://env.invalid:8080"})

    def test_no_proxy_falls_through_to_direct_client(self):
        config = self._config()
        with (
            patch("pixiv_plugin_under_test.core.client.AppPixivAPI") as api,
            patch.object(
                PixivClientWrapper, "_create_direct_client", return_value="direct"
            ) as direct,
        ):
            wrapper = PixivClientWrapper(config)

        direct.assert_called_once()
        api.assert_not_called()
        self.assertEqual(wrapper.client_api, "direct")

    def test_config_proxy_still_authoritative(self):
        config = self._config(
            proxy="http://config.invalid:7890",
            effective_proxy="http://config.invalid:7890",
            get_requests_kwargs=lambda: {
                "proxies": {"http": "http://config.invalid:7890"}
            },
        )
        with patch("pixiv_plugin_under_test.core.client.AppPixivAPI") as api:
            PixivClientWrapper(config)
            api.assert_called_once()

    def test_stub_without_effective_proxy_still_uses_proxy_attribute(self):
        """兼容只有 proxy 属性的替身（如既有测试里的 SimpleNamespace）。"""
        config = SimpleNamespace(
            proxy="http://raw.invalid:8080",
            api_proxy_host="",
            refresh_token="fake-token",
            refresh_interval=180,
            get_requests_kwargs=lambda: {},
        )
        with patch("pixiv_plugin_under_test.core.client.AppPixivAPI") as api:
            PixivClientWrapper(config)
            api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
