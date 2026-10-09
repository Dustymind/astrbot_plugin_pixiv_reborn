"""
珍藏册（Collection）解析的离线单元测试。

fixture 来自真实接口响应（已裁剪掉解析用不到的字段），用于锁定解析契约：
    - 同一作品被引用多次时按 workId 去重
    - 混合珍藏册里插画 / 小说 / 文本块 / 外链的分类
    - 接口错误信封的异常路径
"""

import ast
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
    _extract_recursive_flag,
    format_collection_header,
    format_trailing_sections,
    normalize_collection,
    parse_collection_id,
    resolve_command_args,
)
from pixiv_plugin_under_test.handlers.illust import IllustHandler  # noqa: E402
from pixiv_plugin_under_test.handlers import collection as collection_module  # noqa: E402
from pixiv_plugin_under_test.utils.tag import (  # noqa: E402
    _count_pages,
    _limit_by_page_budget,
)
from pixiv_plugin_under_test.utils.config import PixivConfig  # noqa: E402
from pixiv_plugin_under_test.utils import pixiv_utils as pu  # noqa: E402

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


class RecursiveFlagTests(unittest.TestCase):
    """`-r` 开关的解析。开关位置不限，且不能干扰 ID 提取。"""

    def test_flag_before_id(self):
        recursive, rest = _extract_recursive_flag("-r " + ILLUST_ONLY_ID)
        self.assertTrue(recursive)
        self.assertEqual(rest, ILLUST_ONLY_ID)

    def test_flag_after_id(self):
        recursive, rest = _extract_recursive_flag(ILLUST_ONLY_ID + " -r")
        self.assertTrue(recursive)
        self.assertEqual(rest, ILLUST_ONLY_ID)

    def test_long_flag(self):
        recursive, rest = _extract_recursive_flag("--recursive " + ILLUST_ONLY_ID)
        self.assertTrue(recursive)
        self.assertEqual(rest, ILLUST_ONLY_ID)

    def test_chinese_flag(self):
        recursive, rest = _extract_recursive_flag("递归 " + ILLUST_ONLY_ID)
        self.assertTrue(recursive)
        self.assertEqual(rest, ILLUST_ONLY_ID)

    def test_case_insensitive(self):
        recursive, _ = _extract_recursive_flag("-R " + ILLUST_ONLY_ID)
        self.assertTrue(recursive)

    def test_absent_flag_keeps_text_untouched(self):
        recursive, rest = _extract_recursive_flag(ILLUST_ONLY_ID)
        self.assertFalse(recursive)
        self.assertEqual(rest, ILLUST_ONLY_ID)

    def test_flag_does_not_break_url_parsing(self):
        recursive, rest = _extract_recursive_flag(
            "-r https://www.pixiv.net/collections/" + ILLUST_ONLY_ID
        )
        self.assertTrue(recursive)
        self.assertEqual(parse_collection_id(rest), ILLUST_ONLY_ID)


class ResolveCommandArgsTests(unittest.TestCase):
    """AstrBot 只把第一个 token 绑给形参，其余要从消息文本补齐。

    否则 `/pixiv_collection -r <ID>` 只能拿到 `-r`，ID 被丢弃。
    """

    def _event(self, message_str: str):
        return SimpleNamespace(message_str=message_str)

    def test_recovers_tokens_lost_by_positional_binding(self):
        event = self._event("pixiv_collection -r 99190503582109340029")
        self.assertEqual(
            resolve_command_args(event, "pixiv_collection", "-r"),
            "-r 99190503582109340029",
        )

    def test_keeps_bound_text_when_already_complete(self):
        full = "-r 99190503582109340029"
        event = self._event("pixiv_collection " + full)
        self.assertEqual(resolve_command_args(event, "pixiv_collection", full), full)

    def test_bare_command_yields_empty(self):
        event = self._event("pixiv_collection")
        self.assertEqual(resolve_command_args(event, "pixiv_collection", ""), "")

    def test_tolerates_extra_whitespace(self):
        event = self._event("pixiv_collection    -r   99190503582109340029")
        self.assertEqual(
            resolve_command_args(event, "pixiv_collection", "-r"),
            "-r 99190503582109340029",
        )

    def test_does_not_match_longer_command_names(self):
        # 前缀必须完整匹配，避免 "pixiv_collectionx" 被误当成我们的指令
        event = self._event("pixiv_collectionx -r 1")
        self.assertEqual(resolve_command_args(event, "pixiv_collection", "-r"), "-r")

    def test_falls_back_when_message_is_unrelated(self):
        event = self._event("随便说点什么")
        self.assertEqual(resolve_command_args(event, "pixiv_collection", "abc"), "abc")

    def test_handles_missing_message_str(self):
        event = SimpleNamespace()
        self.assertEqual(resolve_command_args(event, "pixiv_collection", "-r"), "-r")

    def test_flag_parsing_works_on_recovered_text(self):
        event = self._event("pixiv_collection -r 99190503582109340029")
        recovered = resolve_command_args(event, "pixiv_collection", "-r")
        recursive, rest = _extract_recursive_flag(recovered)
        self.assertTrue(recursive)
        self.assertEqual(parse_collection_id(rest), ILLUST_ONLY_ID)


class CommandSignatureTests(unittest.TestCase):
    """静态检查 main.py 里 GreedyStr 的写法。

    必须标注 `GreedyStr` **且不能带默认值**：AstrBot 的 CommandFilter 在形参
    有默认值时取的是默认值而非注解，GreedyStr 会静默失效——这正是
    `/pixiv_collection -r <ID>` 只拿到 `-r` 的原因。用 AST 检查，避免为此导入 main.py。
    """

    GREEDY_COMMANDS = {
        "pixiv_collection",
        "pixiv_fanbox_dl",
        "pixiv_fanbox_dl_view",
    }

    @classmethod
    def _command_handlers(cls) -> dict:
        source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        handlers = {}
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if getattr(decorator.func, "id", None) != "command":
                    continue
                if decorator.args and isinstance(decorator.args[0], ast.Constant):
                    handlers[decorator.args[0].value] = node
        return handlers

    def test_greedy_commands_are_annotated(self):
        handlers = self._command_handlers()

        for name in sorted(self.GREEDY_COMMANDS):
            with self.subTest(command=name):
                node = handlers.get(name)
                self.assertIsNotNone(node, f"main.py 中未找到命令 {name}")

                params = node.args.args
                self.assertGreaterEqual(len(params), 3, "期望 (self, event, args)")
                last = params[-1]

                self.assertIsInstance(
                    last.annotation,
                    ast.Name,
                    f"{name} 的最后一个形参必须标注 GreedyStr",
                )
                self.assertEqual(last.annotation.id, "GreedyStr")

                # 一旦带上默认值，AstrBot 就会取默认值而不是注解，GreedyStr 失效
                self.assertEqual(
                    node.args.defaults,
                    [],
                    f"{name} 的 GreedyStr 形参不能有默认值",
                )

    def test_main_module_defines_greedy_fallback(self):
        """旧版 AstrBot 没有 GreedyStr，必须有兜底定义，否则插件直接加载失败。"""
        source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("except Exception", source)
        self.assertIn("class GreedyStr(str)", source)


class PageBudgetTests(unittest.TestCase):
    """递归模式的累计页数预算。"""

    @staticmethod
    def _work(work_id: str, pages: int):
        return _make_work(work_id, "illust", page_count=pages)

    def test_count_pages_defaults_to_one(self):
        self.assertEqual(_count_pages(SimpleNamespace()), 1)
        self.assertEqual(_count_pages(SimpleNamespace(page_count=0)), 1)
        self.assertEqual(_count_pages(SimpleNamespace(page_count=None)), 1)
        self.assertEqual(_count_pages(SimpleNamespace(page_count=7)), 7)

    def test_no_budget_keeps_everything(self):
        works = [self._work("1", 3), self._work("2", 2)]
        kept, dropped, total = _limit_by_page_budget(works, None)
        self.assertEqual([w.id for w in kept], [1, 2])
        self.assertEqual(dropped, 0)
        self.assertEqual(total, 5)

    def test_truncates_in_collection_order(self):
        works = [self._work("1", 3), self._work("2", 3), self._work("3", 1)]
        kept, dropped, total = _limit_by_page_budget(works, 5)
        # 3 + 3 > 5 → 停在第二个作品
        self.assertEqual([w.id for w in kept], [1])
        self.assertEqual(dropped, 2)
        self.assertEqual(total, 3)

    def test_fits_multiple_works_within_budget(self):
        works = [self._work("1", 2), self._work("2", 2), self._work("3", 2)]
        kept, dropped, total = _limit_by_page_budget(works, 4)
        self.assertEqual([w.id for w in kept], [1, 2])
        self.assertEqual(dropped, 1)
        self.assertEqual(total, 4)

    def test_first_work_kept_even_when_oversized(self):
        works = [self._work("1", 40), self._work("2", 1)]
        kept, dropped, total = _limit_by_page_budget(works, 5)
        self.assertEqual([w.id for w in kept], [1])
        self.assertEqual(dropped, 1)
        self.assertEqual(total, 40)

    def test_zero_budget_means_unlimited(self):
        works = [self._work("1", 3), self._work("2", 3)]
        kept, dropped, _ = _limit_by_page_budget(works, 0)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)


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

    def test_header_marks_recursive_mode(self):
        data = normalize_collection(ILLUST_ONLY_ID, load_fixture("illust_only.json"))

        header = format_collection_header(data, recursive=True, max_pages=50)

        self.assertIn("递归", header)
        self.assertIn("页数上限 50", header)

    def test_header_has_no_mode_line_by_default(self):
        data = normalize_collection(ILLUST_ONLY_ID, load_fixture("illust_only.json"))
        self.assertNotIn("递归", format_collection_header(data))

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


def _make_work(work_id: str, kind: str, page_count: int = 1):
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
        page_count=page_count,
        image_urls=SimpleNamespace(large="https://i.pximg.net/large.jpg"),
    )


class _FakeClientWrapper:
    """替身：记录被请求的作品 ID，并可指定哪些 ID 应失败、各作品的页数。"""

    def __init__(self, fail_ids=(), page_counts=None):
        self.requested = []
        self.fail_ids = set(fail_ids)
        # 按「第几次请求」决定该作品的页数，未指定则 1 页
        self.page_counts = list(page_counts or [])
        self.client_api = SimpleNamespace(
            illust_detail=ILLUST_DETAIL, novel_detail=NOVEL_DETAIL
        )

    async def authenticate(self, **kwargs):
        return True

    async def call_pixiv_api(self, func, *args, **kwargs):
        work_id = str(args[0])
        index = len(self.requested)
        self.requested.append(work_id)
        if work_id in self.fail_ids:
            raise RuntimeError("simulated api failure")
        pages = self.page_counts[index] if index < len(self.page_counts) else 1
        kind = "novel" if func is NOVEL_DETAIL else "illust"
        return SimpleNamespace(**{kind: _make_work(work_id, kind, page_count=pages)})


def _collection_config(**overrides) -> SimpleNamespace:
    config = SimpleNamespace(
        collection_enabled=True,
        collection_max_works=30,
        collection_include_novel=True,
        collection_include_text=True,
        collection_fetch_concurrency=3,
        collection_recursive_max_pages=100,
        collection_recursive_interval_seconds=1,
        proxy="",
        effective_proxy="",
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
            {
                "send_all_pages": kwargs.get("send_all_pages", False),
                "throttle_seconds": kwargs.get("throttle_seconds", 0.0),
            },
        )

    return generator()


def _fake_send_image(client, event, illust, detail_message, **kwargs):
    async def generator():
        yield (
            "image",
            getattr(illust, "id", None),
            detail_message,
            {
                "send_all_pages": kwargs.get("send_all_pages", False),
                "throttle_seconds": kwargs.get("throttle_seconds", 0.0),
            },
        )

    return generator()


class HandleCollectionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run_flow(
        self, fixture_name, collection_id, config, wrapper, *, recursive=False
    ):
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
                async for item in handler.handle_collection_id(
                    event, collection_id, recursive=recursive
                )
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

    async def test_recursive_mode_sends_all_pages_and_throttles(self):
        # 前三个被请求的作品各 3 页
        wrapper = _FakeClientWrapper(page_counts=[3, 3, 3])
        config = _collection_config(
            collection_max_works=50,
            collection_recursive_max_pages=5,
            collection_recursive_interval_seconds=2,
        )

        results = await self._run_flow(
            "illust_only.json", ILLUST_ONLY_ID, config, wrapper, recursive=True
        )

        # 递归模式强制逐条发送，不应出现合并转发
        self.assertEqual([r for r in results if r[0] == "forward"], [])
        images = [r for r in results if r[0] == "image"]
        # 页数预算 5：第一个作品 3 页放行，累加到第二个就 3+3>5，停在这里
        self.assertEqual([r[1] for r in images], [122208118])
        self.assertTrue(images[0][3]["send_all_pages"])
        self.assertEqual(images[0][3]["throttle_seconds"], 2)
        # 信息头里标明递归模式与页数上限
        header = next(
            r[1] for r in results if r[0] == "plain" and "珍藏册" in str(r[1])
        )
        self.assertIn("递归", header)
        self.assertIn("页数上限 5", header)
        # 被预算跳过的作品要有提示
        self.assertTrue(
            any(r[0] == "plain" and "页数上限" in str(r[1]) for r in results)
        )

    async def test_non_recursive_keeps_first_page_only(self):
        wrapper = _FakeClientWrapper()
        config = _collection_config(collection_max_works=50)

        results = await self._run_flow(
            "illust_only.json", ILLUST_ONLY_ID, config, wrapper
        )

        forwards = [r for r in results if r[0] == "forward"]
        self.assertEqual(len(forwards), 1)
        self.assertFalse(forwards[0][3]["send_all_pages"])
        self.assertEqual(forwards[0][3]["throttle_seconds"], 0.0)

    async def test_recursive_mode_respects_forward_threshold(self):
        wrapper = _FakeClientWrapper()
        config = _collection_config(
            collection_max_works=50,
            forward_threshold=True,
            collection_recursive_max_pages=50,
            collection_recursive_interval_seconds=0,
        )

        results = await self._run_flow(
            "illust_only.json", ILLUST_ONLY_ID, config, wrapper, recursive=True
        )

        forwards = [r for r in results if r[0] == "forward"]
        self.assertEqual(len(forwards), 1)
        self.assertTrue(forwards[0][3]["send_all_pages"])

    async def test_recursive_page_budget_never_silently_drops_everything(self):
        # 第一个作品就 40 页，预算 5 —— 仍必须发出第一个作品
        wrapper = _FakeClientWrapper(page_counts=[40])
        config = _collection_config(
            collection_max_works=50,
            collection_recursive_max_pages=5,
            collection_recursive_interval_seconds=0,
        )

        results = await self._run_flow(
            "illust_only.json", ILLUST_ONLY_ID, config, wrapper, recursive=True
        )

        images = [r for r in results if r[0] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0][1], 122208118)

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


class RecursiveConfigTests(unittest.TestCase):
    """递归模式新增配置项的读取与默认值。"""

    def test_defaults(self):
        config = PixivConfig({"refresh_token": "fake"})
        self.assertEqual(config.collection_recursive_max_pages, 100)
        self.assertEqual(config.collection_recursive_interval_seconds, 1)

    def test_explicit_values(self):
        config = PixivConfig(
            {
                "refresh_token": "fake",
                "collection_recursive_max_pages": 42,
                "collection_recursive_interval_seconds": 3,
            }
        )
        self.assertEqual(config.collection_recursive_max_pages, 42)
        self.assertEqual(config.collection_recursive_interval_seconds, 3)

    def test_interval_zero_means_unlimited_and_must_survive(self):
        """0 是「不限速」的合法取值，不能被当成空值替换成默认的 1。"""
        config = PixivConfig(
            {"refresh_token": "fake", "collection_recursive_interval_seconds": 0}
        )
        self.assertEqual(config.collection_recursive_interval_seconds, 0)

    def test_interval_accepts_numeric_string(self):
        config = PixivConfig(
            {"refresh_token": "fake", "collection_recursive_interval_seconds": "5"}
        )
        self.assertEqual(config.collection_recursive_interval_seconds, 5)

    def test_negative_interval_is_clamped_to_zero(self):
        config = PixivConfig(
            {"refresh_token": "fake", "collection_recursive_interval_seconds": -3}
        )
        self.assertEqual(config.collection_recursive_interval_seconds, 0)

    def test_invalid_values_fall_back_to_default(self):
        config = PixivConfig(
            {
                "refresh_token": "fake",
                "collection_recursive_max_pages": "abc",
                "collection_recursive_interval_seconds": None,
            }
        )
        self.assertEqual(config.collection_recursive_max_pages, 100)
        self.assertEqual(config.collection_recursive_interval_seconds, 1)


class ThrottleSleepTests(unittest.IsolatedAsyncioTestCase):
    """递归模式的按张控速：第一张不等待，之后每张之间等待。"""

    async def test_waits_between_items_but_not_before_the_first(self):
        with unittest.mock.patch.object(
            pu.asyncio, "sleep", new=AsyncMock()
        ) as sleep_mock:
            await pu._throttle_sleep(0, 1.0)
            await pu._throttle_sleep(1, 1.0)
            await pu._throttle_sleep(2, 0.5)

        self.assertEqual(
            [call.args[0] for call in sleep_mock.await_args_list], [1.0, 0.5]
        )

    async def test_zero_or_missing_interval_never_sleeps(self):
        with unittest.mock.patch.object(
            pu.asyncio, "sleep", new=AsyncMock()
        ) as sleep_mock:
            await pu._throttle_sleep(1, 0)
            await pu._throttle_sleep(2, 0.0)
            await pu._throttle_sleep(3, None)

        sleep_mock.assert_not_awaited()


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
