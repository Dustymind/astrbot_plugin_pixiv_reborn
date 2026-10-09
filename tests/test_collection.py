"""
珍藏册（Collection）解析的离线单元测试。

fixture 来自真实接口响应（已裁剪掉解析用不到的字段），用于锁定解析契约：
    - 同一作品被引用多次时按 workId 去重
    - 混合珍藏册里插画 / 小说 / 文本块 / 外链的分类
    - 接口错误信封的异常路径
"""

import json
import sys
import types
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def _noop_logger() -> SimpleNamespace:
    return SimpleNamespace(debug=Mock(), info=Mock(), warning=Mock(), error=Mock())


# 1) 保证 astrbot / astrbot.api 可导入
try:
    import astrbot.api  # noqa: F401
except ImportError:
    _astrbot_module = ModuleType("astrbot")
    _api_module = ModuleType("astrbot.api")
    _api_module.logger = _noop_logger()
    _astrbot_module.api = _api_module
    sys.modules["astrbot"] = _astrbot_module
    sys.modules["astrbot.api"] = _api_module

# 2) 保证 astrbot.api.event / astrbot.api.message_components 可导入。
#    其他测试模块可能已装入一个只含 logger 的精简桩，这里必须补齐，
#    否则单独跑没问题、一起跑就 ModuleNotFoundError。
_api_module = sys.modules["astrbot.api"]
if not hasattr(_api_module, "logger"):
    _api_module.logger = _noop_logger()

try:
    import astrbot.api.event  # noqa: F401
except Exception:
    _event_module = ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # 测试替身，仅用于类型占位
        pass

    _event_module.AstrMessageEvent = AstrMessageEvent
    _api_module.event = _event_module
    sys.modules["astrbot.api.event"] = _event_module

try:
    import astrbot.api.message_components  # noqa: F401
except Exception:
    _components_module = ModuleType("astrbot.api.message_components")
    for _name in ("Image", "Plain", "Node", "Nodes"):
        setattr(_components_module, _name, type(_name, (), {}))
    _api_module.message_components = _components_module
    sys.modules["astrbot.api.message_components"] = _components_module

# 插件以「包」的形式被 AstrBot 加载，模块内使用了 `..utils` 这类相对导入。
# 直接 `import handlers.xxx` 会因为没有父包而报
# "attempted relative import beyond top-level package"，
# 因此这里用合成包名挂载插件根目录，不依赖克隆下来的目录名。
_PKG_NAME = "pixiv_plugin_under_test"
if _PKG_NAME not in sys.modules:
    _pkg = types.ModuleType(_PKG_NAME)
    _pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[_PKG_NAME] = _pkg

from pixiv_plugin_under_test.handlers.collection import (  # noqa: E402
    CollectionData,
    CollectionFetchError,
    format_collection_header,
    format_trailing_sections,
    normalize_collection,
    parse_collection_id,
)
from pixiv_plugin_under_test.handlers.illust import IllustHandler  # noqa: E402
from pixiv_plugin_under_test.handlers import collection as collection_module  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "collection"

ILLUST_ONLY_ID = "99190503582109340029"
MIXED_ID = "92396089944566015129"
TEXT_LINK_ONLY_ID = "10229194744398269119"


def load_fixture(name: str) -> dict:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class ParseCollectionIdTests(unittest.TestCase):
    def test_accepts_all_supported_link_forms(self):
        cases = [
            "https://www.pixiv.net/collections/99190503582109340029",
            "https://www.pixiv.net/collection/99190503582109340029",
            "https://www.pixiv.net/users/34862937/collections/99190503582109340029",
            "https://www.pixiv.net/collections/99190503582109340029?p=1",
            "  99190503582109340029  ",
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(parse_collection_id(case), ILLUST_ONLY_ID)

    def test_rejects_inputs_without_collection_id(self):
        cases = [
            "",
            "   ",
            "123",
            "https://www.pixiv.net/collections",
            "https://www.pixiv.net/artworks/122208118",
            "随便说点什么",
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertIsNone(parse_collection_id(case))


class NormalizeCollectionTests(unittest.TestCase):
    def test_illust_only_dedupes_repeated_works(self):
        data = normalize_collection(ILLUST_ONLY_ID, load_fixture("illust_only.json"))

        self.assertEqual(data.tile_count, 27)
        # 27 个格子只对应 12 个不同作品，同作品被引用了多个不同页
        self.assertEqual(len(data.illust_ids), 12)
        self.assertEqual(len(set(data.illust_ids)), 12)
        self.assertEqual(data.novel_ids, [])
        self.assertEqual(data.texts, [])
        self.assertEqual(data.links, [])

        self.assertEqual(data.title, "お気に入りゆるゆりFA")
        self.assertEqual(data.author, "街山あかね")
        self.assertEqual(data.author_id, "34862937")
        self.assertEqual(data.bookmark_count, 173)
        self.assertEqual(data.view_count, 47894)
        self.assertEqual(data.status, "public")
        self.assertEqual(data.x_restrict, 0)
        self.assertIn("ゆるゆり", data.tags)

        # 保持珍藏册排版顺序，并记录首次被引用的页码（0-based）
        self.assertEqual(data.illust_ids[0], "122208118")
        self.assertEqual(data.illust_quote_pages["122208118"], 4)

    def test_mixed_collection_splits_tile_types(self):
        data = normalize_collection(
            MIXED_ID, load_fixture("mixed_novel_text_link.json")
        )

        self.assertEqual(data.tile_count, 41)
        self.assertEqual(len(data.illust_ids), 6)
        self.assertEqual(len(data.novel_ids), 9)
        # 20 个 Text 格子里有 1 个是布局占位（text 为空串），被跳过；
        # 剩余 19 条里有 9 条是同一个标签被重复贴，按首次出现去重后为 10 条。
        self.assertEqual(len(data.texts), 10)
        self.assertEqual(len(data.links), 6)
        self.assertEqual(data.total_works, 15)
        self.assertIn("さなコン", data.tags)

    def test_blank_and_duplicated_text_tiles_are_collapsed(self):
        envelope = {
            "error": False,
            "message": "",
            "body": {
                "data": {
                    "detail": {
                        "tiles": [
                            {"type": "Text", "text": "   "},
                            {"type": "Text", "text": "小説"},
                            {"type": "Text", "text": "小説"},
                            {"type": "Text", "text": "マンガ"},
                            {"type": "ExternalUrl", "url": "https://a.example"},
                            {"type": "ExternalUrl", "url": "https://a.example"},
                            {"type": "ExternalUrl", "url": "https://b.example"},
                        ]
                    },
                    "userCollections": {"1": {"title": "t"}},
                }
            },
        }
        data = normalize_collection("1", envelope)

        self.assertEqual(data.texts, ["小説", "マンガ"])
        self.assertEqual(
            [link["url"] for link in data.links],
            ["https://a.example", "https://b.example"],
        )

    def test_collection_without_illusts(self):
        data = normalize_collection(
            TEXT_LINK_ONLY_ID, load_fixture("text_link_only.json")
        )

        self.assertEqual(data.tile_count, 16)
        self.assertEqual(data.illust_ids, [])
        self.assertEqual(len(data.novel_ids), 1)
        self.assertEqual(len(data.texts), 12)
        self.assertEqual(len(data.links), 3)
        self.assertEqual(data.total_works, 1)

    def test_work_tiles_without_work_id_are_skipped(self):
        envelope = {
            "error": False,
            "message": "",
            "body": {
                "data": {
                    "detail": {
                        "tiles": [
                            {"type": "Work", "workType": "illust"},
                            {"type": "Work", "workType": "illust", "workId": "555"},
                            {"type": "Work", "workType": "illust", "workId": "555"},
                        ]
                    },
                    "userCollections": {"9": {"title": "t", "userName": "u"}},
                }
            },
        }
        data = normalize_collection("9", envelope)
        self.assertEqual(data.illust_ids, ["555"])
        self.assertEqual(data.tile_count, 3)

    def test_error_envelope_raises(self):
        with self.assertRaises(CollectionFetchError):
            normalize_collection("1", {"error": True, "message": "boom", "body": []})

    def test_missing_body_raises(self):
        with self.assertRaises(CollectionFetchError):
            normalize_collection("1", {"error": False, "message": "", "body": None})

    def test_falls_back_when_meta_key_mismatch(self):
        envelope = {
            "error": False,
            "message": "",
            "body": {
                "data": {
                    "detail": {"tiles": []},
                    "userCollections": {
                        "111": {"title": "标题", "userName": "作者", "tags": ["a"]}
                    },
                }
            },
        }
        data = normalize_collection("999", envelope)
        self.assertEqual(data.title, "标题")
        self.assertEqual(data.author, "作者")
        self.assertEqual(data.tags, ["a"])


class FormattingTests(unittest.TestCase):
    def test_header_carries_collection_metadata(self):
        data = normalize_collection(ILLUST_ONLY_ID, load_fixture("illust_only.json"))
        header = format_collection_header(data)

        self.assertIn("お気に入りゆるゆりFA", header)
        self.assertIn("街山あかね", header)
        self.assertIn("34862937", header)
        self.assertIn("收藏 173", header)
        self.assertIn("https://www.pixiv.net/collections/99190503582109340029", header)

    def test_trailing_sections_render_texts_and_links(self):
        data = normalize_collection(
            TEXT_LINK_ONLY_ID, load_fixture("text_link_only.json")
        )
        joined = "\n".join(format_trailing_sections(data))

        self.assertIn("文本块", joined)
        self.assertIn("外链", joined)
        self.assertIn("https://booth.pm", joined)

    def test_trailing_sections_empty_for_illust_only_collection(self):
        data = normalize_collection(ILLUST_ONLY_ID, load_fixture("illust_only.json"))
        self.assertEqual(format_trailing_sections(data), [])

    def test_oversized_text_and_link_lists_are_truncated(self):
        data = CollectionData(
            collection_id="1",
            texts=[f"text-{i}" for i in range(30)],
            links=[{"url": f"https://example.com/{i}", "title": ""} for i in range(30)],
        )
        joined = "\n".join(format_trailing_sections(data))

        self.assertIn("另有 10 个文本块未列出", joined)
        self.assertIn("另有 10 条外链未列出", joined)

    def test_single_text_block_over_limit_is_clipped(self):
        data = CollectionData(collection_id="1", texts=["x" * 500])
        joined = "\n".join(format_trailing_sections(data))

        self.assertIn("…", joined)
        self.assertNotIn("x" * 500, joined)


class _FakeCollectionHandler:
    def __init__(self):
        self.seen = []

    async def handle_collection_id(self, event, collection_id):
        self.seen.append(collection_id)
        yield "COLLECTED"


# --- 编排流程测试：抓取 → 去重 → 补全详情 → 过滤 → 截断 → 转发 ---

ILLUST_DETAIL = object()
NOVEL_DETAIL = object()


def _make_work(work_id: str, kind: str):
    return SimpleNamespace(
        id=int(work_id),
        title=f"{kind}-{work_id}",
        user=SimpleNamespace(name="tester"),
        tags=[],
        x_restrict=0,
        illust_ai_type=0,
        total_bookmarks=0,
        total_view=0,
        type="illust",
        page_count=1,
        image_urls=SimpleNamespace(large="https://i.pximg.net/large.jpg"),
    )


class _FakeClientWrapper:
    """替身：记录被请求的作品 ID，并可指定哪些 ID 应失败。"""

    def __init__(self, fail_ids=()):
        self.requested = []
        self.fail_ids = set(fail_ids)
        self.client_api = SimpleNamespace(
            illust_detail=ILLUST_DETAIL, novel_detail=NOVEL_DETAIL
        )

    async def authenticate(self, **kwargs):
        return True

    async def call_pixiv_api(self, func, *args, **kwargs):
        work_id = str(args[0])
        self.requested.append(work_id)
        if work_id in self.fail_ids:
            raise RuntimeError("simulated api failure")
        kind = "novel" if func is NOVEL_DETAIL else "illust"
        return SimpleNamespace(**{kind: _make_work(work_id, kind)})


def _collection_config(**overrides) -> SimpleNamespace:
    config = SimpleNamespace(
        collection_enabled=True,
        collection_max_works=30,
        collection_include_novel=True,
        collection_include_text=True,
        collection_fetch_concurrency=3,
        proxy="",
        r18_mode="过滤 R18",
        filter_r18g_only=False,
        ai_filter_mode="显示 AI 作品",
        ai_detection_mode="field_or_tag",
        show_filter_result=True,
        single_response_mode=True,
        show_details=True,
        forward_threshold=False,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    config.get_auth_error_message = lambda: "auth error"
    return config


def _fake_forward(client, event, images, build_detail_message_func, **kwargs):
    async def generator():
        yield (
            "forward",
            [getattr(image, "id", None) for image in images],
            kwargs.get("summary_text"),
        )

    return generator()


def _fake_send_image(client, event, illust, detail_message, **kwargs):
    async def generator():
        yield ("image", getattr(illust, "id", None))

    return generator()


class HandleCollectionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run_flow(self, fixture_name, collection_id, config, wrapper):
        handler = collection_module.CollectionHandler(wrapper, config)
        event = _make_event("")
        envelope = load_fixture(fixture_name)

        with (
            unittest.mock.patch.object(
                collection_module,
                "fetch_collection",
                new=AsyncMock(return_value=envelope),
            ),
            unittest.mock.patch.object(
                collection_module, "send_forward_message", new=_fake_forward
            ),
            unittest.mock.patch.object(
                collection_module, "send_pixiv_image", new=_fake_send_image
            ),
            # 关掉限速间隔，避免单元测试白等
            unittest.mock.patch.object(
                collection_module, "DETAIL_REQUEST_MIN_INTERVAL", 0
            ),
        ):
            results = [
                item
                async for item in handler.handle_collection_id(event, collection_id)
            ]
        return results

    async def test_flow_dedupes_and_caps_at_max_works(self):
        wrapper = _FakeClientWrapper()
        config = _collection_config(collection_max_works=5)

        results = await self._run_flow(
            "illust_only.json", ILLUST_ONLY_ID, config, wrapper
        )

        forwards = [r for r in results if r[0] == "forward"]
        self.assertEqual(len(forwards), 1)
        sent_ids = forwards[0][1]
        # 珍藏册里有 12 个去重作品，发送上限 5 个
        self.assertEqual(len(sent_ids), 5)
        # 详情是按珍藏册排版顺序抓取的（去重后 12 个全抓，再按上限截断）
        self.assertEqual(len(wrapper.requested), 12)
        self.assertEqual(wrapper.requested[:5], [str(i) for i in sent_ids])
        summary = forwards[0][2]
        self.assertIn("お気に入りゆるゆりFA", summary)
        self.assertIn("珍藏册", summary)

    async def test_flow_requests_every_distinct_work_once(self):
        wrapper = _FakeClientWrapper()
        config = _collection_config(collection_max_works=50)

        await self._run_flow("illust_only.json", ILLUST_ONLY_ID, config, wrapper)

        # 27 个格子 → 12 个不同作品，不能重复请求
        self.assertEqual(len(wrapper.requested), 12)
        self.assertEqual(len(set(wrapper.requested)), 12)

    async def test_flow_splits_illust_and_novel_blocks(self):
        wrapper = _FakeClientWrapper()
        config = _collection_config(collection_max_works=50)

        results = await self._run_flow(
            "mixed_novel_text_link.json", MIXED_ID, config, wrapper
        )

        forwards = [r for r in results if r[0] == "forward"]
        self.assertEqual(len(forwards), 2)
        illust_ids, novel_ids = forwards[0][1], forwards[1][1]
        self.assertEqual(len(illust_ids), 6)
        self.assertEqual(len(novel_ids), 9)
        # 插画块的汇总里带珍藏册信息头与文本/外链摘要
        self.assertIn("文本块", forwards[0][2])
        self.assertIn("外链", forwards[0][2])
        # 小说块只带一行上下文，不重复整段信息头
        secondary_head = forwards[1][2].split("搜索完成")[0].strip()
        self.assertIn("さなコン１、２受賞作コレクション", secondary_head)
        self.assertIn("小说部分", secondary_head)
        self.assertNotIn("内容: 格子", secondary_head)

    async def test_flow_skips_novels_when_disabled(self):
        wrapper = _FakeClientWrapper()
        config = _collection_config(
            collection_max_works=50, collection_include_novel=False
        )

        results = await self._run_flow(
            "mixed_novel_text_link.json", MIXED_ID, config, wrapper
        )

        forwards = [r for r in results if r[0] == "forward"]
        self.assertEqual(len(forwards), 1)
        self.assertEqual(len(forwards[0][1]), 6)
        # 小说不应被请求详情
        novel_ids = set(
            normalize_collection(
                MIXED_ID, load_fixture("mixed_novel_text_link.json")
            ).novel_ids
        )
        self.assertFalse(novel_ids & set(wrapper.requested))

    async def test_flow_tolerates_failed_detail_requests(self):
        wrapper = _FakeClientWrapper(fail_ids={"122208118"})
        config = _collection_config(collection_max_works=50)

        results = await self._run_flow(
            "illust_only.json", ILLUST_ONLY_ID, config, wrapper
        )

        forwards = [r for r in results if r[0] == "forward"]
        self.assertEqual(len(forwards), 1)
        # 12 个作品里有 1 个补全失败，其余照常发送
        self.assertEqual(len(forwards[0][1]), 11)
        self.assertNotIn(122208118, forwards[0][1])

    async def test_flow_falls_back_to_text_list_when_sending_fails(self):
        wrapper = _FakeClientWrapper()
        config = _collection_config(collection_max_works=50)
        handler = collection_module.CollectionHandler(wrapper, config)
        event = _make_event("")

        async def exploding_forward(*args, **kwargs):
            raise RuntimeError("simulated forward failure")
            yield  # pragma: no cover

        with (
            unittest.mock.patch.object(
                collection_module,
                "fetch_collection",
                new=AsyncMock(return_value=load_fixture("illust_only.json")),
            ),
            unittest.mock.patch.object(
                collection_module, "send_forward_message", new=exploding_forward
            ),
            unittest.mock.patch.object(
                collection_module, "DETAIL_REQUEST_MIN_INTERVAL", 0
            ),
        ):
            results = [
                item
                async for item in handler.handle_collection_id(event, ILLUST_ONLY_ID)
            ]

        # 发送链路炸了也要降级出文本清单，而不是把异常抛给事件总线
        self.assertEqual(len(results), 1)
        kind, message = results[0]
        self.assertEqual(kind, "plain")
        self.assertIn("插画图片发送失败", message)
        self.assertIn("https://www.pixiv.net/artworks/", message)

    async def test_flow_reports_error_message_on_fetch_failure(self):
        handler = collection_module.CollectionHandler(
            _FakeClientWrapper(), _collection_config()
        )
        event = _make_event("")

        with unittest.mock.patch.object(
            collection_module,
            "fetch_collection",
            new=AsyncMock(
                side_effect=CollectionFetchError("珍藏册不存在、已被删除，或未公开。")
            ),
        ):
            results = [
                item
                async for item in handler.handle_collection_id(event, ILLUST_ONLY_ID)
            ]

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "plain")
        self.assertIn("珍藏册不存在", results[0][1])

    async def test_flow_handles_collection_with_only_text_and_links(self):
        wrapper = _FakeClientWrapper()
        config = _collection_config(collection_max_works=50)

        results = await self._run_flow(
            "text_link_only.json", TEXT_LINK_ONLY_ID, config, wrapper
        )

        # 无插画，仅 1 个小说；信息头与文本/外链摘要挂在同一个转发里
        forwards = [r for r in results if r[0] == "forward"]
        self.assertEqual(len(forwards), 1)
        self.assertEqual(forwards[0][1], [25718148])
        self.assertEqual(wrapper.requested, ["25718148"])

        summary = forwards[0][2]
        self.assertIn("文学フリマ", summary)
        self.assertIn("文本块", summary)
        self.assertIn("https://booth.pm", summary)


def _make_event(text: str) -> SimpleNamespace:
    event = SimpleNamespace()
    event.message_str = text
    event.plain_result = lambda message: ("plain", message)
    return event


class UrlRoutingTests(unittest.IsolatedAsyncioTestCase):
    """验证 URL 自动识别会把珍藏册链接路由给 CollectionHandler。"""

    def _handler(self, fake, *, collection_enabled=True):
        config = SimpleNamespace(
            collection_enabled=collection_enabled,
            pixiv_urlsearch_enabled=True,
        )
        return IllustHandler(SimpleNamespace(client_api=object()), config, fake)

    async def test_collection_url_is_routed(self):
        fake = _FakeCollectionHandler()
        handler = self._handler(fake)
        event = _make_event("https://www.pixiv.net/collections/99190503582109340029")

        results = [r async for r in handler.pixiv_msg_url(event, event.message_str)]

        self.assertEqual(results, ["COLLECTED"])
        self.assertEqual(fake.seen, [ILLUST_ONLY_ID])

    async def test_user_scoped_collection_url_is_routed(self):
        fake = _FakeCollectionHandler()
        handler = self._handler(fake)
        event = _make_event(
            "https://www.pixiv.net/users/34862937/collections/99190503582109340029"
        )

        results = [r async for r in handler.pixiv_msg_url(event, event.message_str)]

        self.assertEqual(results, ["COLLECTED"])
        self.assertEqual(fake.seen, [ILLUST_ONLY_ID])

    async def test_disabled_collection_feature_ignores_link(self):
        fake = _FakeCollectionHandler()
        handler = self._handler(fake, collection_enabled=False)
        event = _make_event("https://www.pixiv.net/collections/99190503582109340029")

        results = [r async for r in handler.pixiv_msg_url(event, event.message_str)]

        self.assertEqual(results, [])
        self.assertEqual(fake.seen, [])

    async def test_non_collection_message_is_not_routed(self):
        fake = _FakeCollectionHandler()
        handler = self._handler(fake)
        event = _make_event("https://www.pixiv.net/artworks/122208118")

        results = [r async for r in handler.pixiv_msg_url(event, event.message_str)]

        # artworks 链接不走珍藏册分支；这里 client 是空壳，不会真的发图
        self.assertEqual(fake.seen, [])
        self.assertNotIn("COLLECTED", results)


class FallbackTextTests(unittest.TestCase):
    """发送失败降级文案。"""

    def test_illust_fallback_uses_artwork_urls(self):
        text = collection_module.CollectionHandler._build_fallback_text(
            [_make_work("122208118", "illust")], False
        )
        self.assertIn("插画图片发送失败", text)
        self.assertIn("https://www.pixiv.net/artworks/122208118", text)

    def test_novel_fallback_uses_novel_urls(self):
        text = collection_module.CollectionHandler._build_fallback_text(
            [_make_work("15362430", "novel")], True
        )
        self.assertIn("小说图片发送失败", text)
        self.assertIn("https://www.pixiv.net/novel/show.php?id=15362430", text)

    def test_fallback_list_is_capped(self):
        works = [_make_work(str(i), "illust") for i in range(1, 40)]
        text = collection_module.CollectionHandler._build_fallback_text(works, False)
        # 39 个作品，最多列 30 个
        self.assertIn("另有 9 个未列出", text)


if __name__ == "__main__":
    unittest.main()
