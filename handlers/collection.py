"""
handlers/collection.py
Pixiv 珍藏册（Collection / コレクション，中文官方译名「珍藏册」）解析。

为什么不能用 pixivpy3：
    珍藏册功能仅存在于 pixiv 网页版。官方帮助中心明确「无法通过 APP 版 pixiv 浏览或
    投稿」，而 pixivpy3 封装的是 App API（app-api.pixiv.net）。实测 App API 的
    /v1/collection/* 与 /v2/collection/* 全部返回 404「指定されたエンドポイントは
    存在しません」，即端点不存在。因此升级 pixivpy3 永远无法解决。

所以本模块直接调用网页版前端使用的 AJAX 接口：
    GET https://www.pixiv.net/ajax/collection/{collectionId}
该接口匿名可读（无需登录、无需 Cookie），但属于非官方内部接口，pixiv 改版即可能
失效，且受到 Cloudflare WAF 保护。

珍藏册不是「作品列表」而是拼贴版式，一个格子（tile）可以是：
    - Work       作品，workType 为 illust / novel
    - Text       纯文本块
    - ExternalUrl 外部链接
同一个作品可以被引用多次（引用不同页），因此必须按 workId 去重，
被引用的页码记录在 tile 的 quotePage 字段（0-based，可能缺省）。
"""

import asyncio
import json
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..utils.help import get_help_message
from ..utils.pixiv_utils import send_forward_message, send_pixiv_image
from ..utils.tag import (
    FilterConfig,
    build_detail_message,
    process_and_send_illusts_sorted,
)


COLLECTION_API_TEMPLATE = "https://www.pixiv.net/ajax/collection/{collection_id}"
COLLECTION_PAGE_TEMPLATE = "https://www.pixiv.net/collections/{collection_id}"

# 匹配 /collections/{id}、/collection/{id}、/users/{uid}/collections/{id}
# 珍藏册 ID 是大整数（实测 20 位），这里放宽到 4 位以上即可，避免误伤其他路径。
COLLECTION_URL_RE = re.compile(r"collections?/(\d{4,})", re.IGNORECASE)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FETCH_TIMEOUT_SECONDS = 30

# 单个珍藏册最多补全多少个作品的详情，防止极端收藏量把 API 打爆。
MAX_FETCH_PER_COLLECTION = 100

# 补全详情时的最小请求间隔（秒），配合 Semaphore 做温和限速。
# pixiv 会对大批量枚举行为吊销 refresh_token，宁可慢一点。
DETAIL_REQUEST_MIN_INTERVAL = 0.2

# 文本块/外链摘要的输出上限
MAX_TEXT_BLOCKS_IN_SUMMARY = 20
MAX_TEXT_BLOCK_CHARS = 200
MAX_LINKS_IN_SUMMARY = 20

# 发送失败降级为文本清单时，最多列出多少个作品
MAX_FALLBACK_ITEMS = 30

# 递归模式的命令行开关
RECURSIVE_FLAGS = ("-r", "--recursive", "递归")


class CollectionFetchError(Exception):
    """珍藏册抓取失败。message 面向用户，直接可展示。"""

    def __init__(self, message: str, *, not_found: bool = False):
        super().__init__(message)
        self.message = message
        self.not_found = not_found


@dataclass
class CollectionData:
    """归一化后的珍藏册数据"""

    collection_id: str
    title: str = "未知珍藏册"
    author: str = "未知"
    author_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    caption: str = ""
    bookmark_count: Optional[int] = None
    view_count: Optional[int] = None
    x_restrict: int = 0
    status: str = "public"
    tile_count: int = 0
    # 按珍藏册排版顺序排列、已去重的作品 ID
    illust_ids: List[str] = field(default_factory=list)
    novel_ids: List[str] = field(default_factory=list)
    # workId -> 首次被引用的页码（0-based）
    illust_quote_pages: Dict[str, int] = field(default_factory=dict)
    texts: List[str] = field(default_factory=list)
    links: List[Dict[str, str]] = field(default_factory=list)

    @property
    def total_works(self) -> int:
        return len(self.illust_ids) + len(self.novel_ids)


def parse_collection_id(text: str) -> Optional[str]:
    """从珍藏册链接或裸 ID 中取出 collectionId，取不到返回 None。"""
    if not text:
        return None
    stripped = text.strip()
    if stripped.isdigit() and len(stripped) >= 4:
        return stripped
    match = COLLECTION_URL_RE.search(stripped)
    return match.group(1) if match else None


def _extract_recursive_flag(text: str) -> tuple[bool, str]:
    """剥离命令参数里的递归开关，返回 (是否递归, 剩余文本)。

    开关位置不限，`/pixiv_collection -r <ID>` 与 `/pixiv_collection <ID> -r` 等价。
    """
    recursive = False
    remaining: List[str] = []
    for token in (text or "").split():
        if token.lower() in RECURSIVE_FLAGS:
            recursive = True
        else:
            remaining.append(token)
    return recursive, " ".join(remaining)


def resolve_command_args(event, command_name: str, bound: str) -> str:
    """取出指令之后的完整参数文本（含 `-r` 这类开关）。

    为什么需要这个：AstrBot 的 `CommandFilter` 会把指令之后的文本按空白切开，
    再**按位置**逐个绑定到处理函数的形参上，多出来的 token 直接丢弃
    （见 `astrbot/core/star/filter/command.py` 的 `validate_and_convert_params`）。
    所以对 `/pixiv_collection -r <ID>` 这种「开关 + 值」的写法，
    只声明一个形参时只能拿到 `-r`，ID 会被丢掉。

    主修法是在函数签名上标注 `GreedyStr`（AstrBot 的官方机制，会把剩余文本
    合并成一个字符串）。本函数是兜底：从 `event.message_str` 补齐，以便在没有
    `GreedyStr` 的旧版 AstrBot 上也能正确工作。唤醒前缀已在 AstrBot 的
    waking_check 阶段剥离，因此 message_str 形如 `pixiv_collection -r 123`。
    """
    raw = str(getattr(event, "message_str", "") or "").strip()
    bound_text = str(bound or "").strip()

    first, _, rest = raw.partition(" ")
    if first == command_name:
        # 归一化连续空白，与 AstrBot 自身的参数处理保持一致
        rest = " ".join(rest.split())
        if len(rest) > len(bound_text):
            return rest

    return bound_text


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


async def fetch_collection(
    collection_id: str,
    proxy: str = "",
    timeout: int = FETCH_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """调用珍藏册 Web AJAX 接口，返回完整响应信封 {error, message, body}。"""
    url = COLLECTION_API_TEMPLATE.format(collection_id=collection_id)
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": COLLECTION_PAGE_TEMPLATE.format(collection_id=collection_id),
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=10, sock_read=20)

    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(
                url,
                headers=headers,
                proxy=proxy or None,
                timeout=client_timeout,
            ) as response:
                status = response.status
                text = await response.text()
    except asyncio.TimeoutError:
        raise CollectionFetchError(
            "抓取珍藏册超时，请稍后重试。"
            "若本机无法直连 pixiv，请在插件配置里填写 `proxy`（如 http://127.0.0.1:7890）。"
        )
    except aiohttp.ClientError as exc:
        raise CollectionFetchError(
            f"抓取珍藏册失败（网络异常）：{exc}"
            "。若本机无法直连 pixiv，请检查插件配置里的 `proxy`。"
        )

    if status == 404:
        raise CollectionFetchError(
            "珍藏册不存在、已被删除，或未公开（私密珍藏册需要登录态，本插件无法读取）。",
            not_found=True,
        )
    if status in (403, 429):
        raise CollectionFetchError(
            f"珍藏册接口被 pixiv 拒绝（HTTP {status}），可能触发了风控，请稍后重试或更换代理。"
        )
    if status != 200:
        raise CollectionFetchError(f"抓取珍藏册失败，HTTP {status}。")
    if text.lstrip().startswith("<"):
        raise CollectionFetchError(
            "珍藏册接口被 Cloudflare 拦截（返回了 HTML 而非 JSON），请更换代理后重试。"
        )

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        raise CollectionFetchError(
            "珍藏册接口返回了非 JSON 内容，该非官方接口可能已被 pixiv 改版失效。"
        )

    if not isinstance(envelope, dict):
        raise CollectionFetchError("珍藏册接口返回结构异常。")
    return envelope


def normalize_collection(
    collection_id: str, envelope: Dict[str, Any]
) -> CollectionData:
    """把接口响应归一化成 CollectionData。"""
    if envelope.get("error"):
        message = str(envelope.get("message") or "").strip()
        raise CollectionFetchError(
            f"珍藏册接口返回错误：{message}" if message else "珍藏册接口返回错误。"
        )

    body = envelope.get("body")
    if not isinstance(body, dict):
        raise CollectionFetchError("珍藏册接口缺少 body 数据。")

    data = body.get("data") or {}
    detail = data.get("detail") or {}
    tiles = detail.get("tiles") or []

    user_collections = data.get("userCollections") or {}
    meta: Dict[str, Any] = {}
    if isinstance(user_collections, dict):
        meta = user_collections.get(collection_id) or {}
        # 接口以 collectionId 为 key；万一 key 形态不一致，退化为取第一个值
        if not meta and len(user_collections) == 1:
            only_value = next(iter(user_collections.values()))
            meta = only_value if isinstance(only_value, dict) else {}

    raw_tags = meta.get("tags") or []
    tags = (
        [str(t).strip() for t in raw_tags if str(t).strip()]
        if isinstance(raw_tags, list)
        else []
    )

    result = CollectionData(
        collection_id=collection_id,
        title=str(meta.get("title") or "").strip() or "未知珍藏册",
        author=str(meta.get("userName") or "").strip() or "未知",
        author_id=(
            str(meta.get("userId")).strip() if meta.get("userId") is not None else None
        ),
        tags=tags,
        caption=str(meta.get("caption") or "").strip(),
        bookmark_count=_as_int(meta.get("bookmarkCount")),
        view_count=_as_int(meta.get("viewCount")),
        x_restrict=_as_int(meta.get("xRestrict")) or 0,
        status=str(meta.get("status") or "public").strip(),
        tile_count=len(tiles) if isinstance(tiles, list) else 0,
    )

    seen_illust: set[str] = set()
    seen_novel: set[str] = set()
    # 珍藏册是拼贴版式，同一个文本标签 / 外链会被重复贴上去，
    # 摘要是给人读的，按首次出现顺序去重，避免刷屏。
    seen_text: set[str] = set()
    seen_link: set[str] = set()

    for tile in tiles:
        if not isinstance(tile, dict):
            continue
        tile_type = tile.get("type")
        if tile_type == "Work":
            work_id = tile.get("workId")
            if work_id is None:
                continue
            work_id = str(work_id)
            work_type = tile.get("workType")
            if work_type == "novel":
                if work_id not in seen_novel:
                    seen_novel.add(work_id)
                    result.novel_ids.append(work_id)
            else:
                # 缺省按插画处理；实测珍藏册内 Work 的 workType 只有 illust / novel
                if work_id not in seen_illust:
                    seen_illust.add(work_id)
                    result.illust_ids.append(work_id)
                    quote_page = _as_int(tile.get("quotePage"))
                    if quote_page is not None:
                        result.illust_quote_pages[work_id] = quote_page
        elif tile_type == "Text":
            text = str(tile.get("text") or "").strip()
            if text and text not in seen_text:
                seen_text.add(text)
                result.texts.append(text)
        elif tile_type == "ExternalUrl":
            url = str(tile.get("url") or "").strip()
            if url and url not in seen_link:
                seen_link.add(url)
                result.links.append(
                    {"url": url, "title": str(tile.get("title") or "").strip()}
                )

    return result


def format_collection_header(
    data: CollectionData, *, recursive: bool = False, max_pages: Optional[int] = None
) -> str:
    """构建珍藏册的信息头。"""
    lines = ["📚 Pixiv 珍藏册", f"标题: {data.title}"]
    author_line = f"作者: {data.author}"
    if data.author_id:
        author_line += f" (ID: {data.author_id})"
    lines.append(author_line)

    if data.tags:
        lines.append("标签: " + ", ".join(data.tags))
    if data.caption:
        lines.append(f"简介: {data.caption}")

    counts = [f"格子 {data.tile_count}"]
    if data.illust_ids:
        counts.append(f"插画 {len(data.illust_ids)}")
    if data.novel_ids:
        counts.append(f"小说 {len(data.novel_ids)}")
    if data.texts:
        counts.append(f"文本 {len(data.texts)}")
    if data.links:
        counts.append(f"外链 {len(data.links)}")
    lines.append("内容: " + " / ".join(counts))

    if recursive:
        mode_line = "模式: 递归（发送每个作品的全部页面）"
        if max_pages:
            mode_line += f"，页数上限 {max_pages}"
        lines.append(mode_line)

    stats = []
    if data.bookmark_count is not None:
        stats.append(f"收藏 {data.bookmark_count}")
    if data.view_count is not None:
        stats.append(f"浏览 {data.view_count}")
    if stats:
        lines.append(" · ".join(stats))

    lines.append(
        "链接: " + COLLECTION_PAGE_TEMPLATE.format(collection_id=data.collection_id)
    )
    return "\n".join(lines)


def format_trailing_sections(data: CollectionData) -> List[str]:
    """构建文本块与外链的摘要段落。"""
    sections: List[str] = []

    if data.texts:
        lines = ["📝 珍藏册文本块:"]
        for text in data.texts[:MAX_TEXT_BLOCKS_IN_SUMMARY]:
            clipped = text
            if len(clipped) > MAX_TEXT_BLOCK_CHARS:
                clipped = clipped[:MAX_TEXT_BLOCK_CHARS] + "…"
            lines.append(f"· {clipped}")
        remaining = len(data.texts) - MAX_TEXT_BLOCKS_IN_SUMMARY
        if remaining > 0:
            lines.append(f"… 另有 {remaining} 个文本块未列出")
        sections.append("\n".join(lines))

    if data.links:
        lines = ["🔗 珍藏册外链:"]
        for link in data.links[:MAX_LINKS_IN_SUMMARY]:
            title = link.get("title") or link.get("url") or ""
            lines.append(f"· {title}\n  {link.get('url', '')}")
        remaining = len(data.links) - MAX_LINKS_IN_SUMMARY
        if remaining > 0:
            lines.append(f"… 另有 {remaining} 条外链未列出")
        sections.append("\n".join(lines))

    return sections


class CollectionHandler:
    """珍藏册解析与发送"""

    def __init__(self, client_wrapper, pixiv_config):
        self.client_wrapper = client_wrapper
        self.client = client_wrapper.client_api
        self.pixiv_config = pixiv_config

    async def pixiv_collection(self, event: AstrMessageEvent, arg: str = ""):
        """处理 /pixiv_collection 命令

        支持 `-r` 开关：递归发送每个作品的全部页面（行为同逐个 /pixiv_specific）。
        """
        cleaned = (arg or "").strip()

        if cleaned.lower() == "help":
            yield event.plain_result(
                get_help_message(
                    "pixiv_collection", "珍藏册帮助消息加载失败，请检查配置文件。"
                )
            )
            return

        if not self.pixiv_config.collection_enabled:
            yield event.plain_result("珍藏册解析功能已在配置中关闭。")
            return

        recursive, cleaned = _extract_recursive_flag(cleaned)

        collection_id = parse_collection_id(cleaned)
        if not collection_id:
            yield event.plain_result(
                get_help_message(
                    "pixiv_collection", "珍藏册帮助消息加载失败，请检查配置文件。"
                )
            )
            return

        async for result in self.handle_collection_id(
            event, collection_id, recursive=recursive
        ):
            yield result

    async def handle_collection_id(
        self,
        event: AstrMessageEvent,
        collection_id: str,
        *,
        recursive: bool = False,
    ):
        """抓取并发送一个珍藏册。URL 自动识别与命令都走这里。"""
        if not self.pixiv_config.collection_enabled:
            return

        logger.info(
            f"Pixiv 插件：正在解析珍藏册 - ID: {collection_id}，递归模式: {recursive}"
        )

        proxy = self.pixiv_config.effective_proxy
        logger.debug(f"Pixiv 插件：珍藏册抓取代理 = {proxy or '未使用（直连）'}")

        try:
            envelope = await fetch_collection(collection_id, proxy=proxy)
            data = normalize_collection(collection_id, envelope)
        except CollectionFetchError as exc:
            logger.warning(
                f"Pixiv 插件：珍藏册 {collection_id} 抓取失败 - {exc.message}"
            )
            yield event.plain_result(exc.message)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Pixiv 插件：解析珍藏册 {collection_id} 时发生异常 - {exc}")
            yield event.plain_result("解析珍藏册时发生未知错误，请查看日志。")
            return

        header = format_collection_header(
            data,
            recursive=recursive,
            max_pages=(
                self.pixiv_config.collection_recursive_max_pages if recursive else None
            ),
        )

        if self.pixiv_config.collection_include_text:
            trailing = format_trailing_sections(data)
            body_text = "\n\n".join([header] + trailing) if trailing else header
        else:
            body_text = header

        if data.total_works == 0:
            if self.pixiv_config.collection_include_text and (data.texts or data.links):
                yield event.plain_result(body_text)
            else:
                yield event.plain_result(
                    f"{header}\n\n该珍藏册内没有可解析的插画或小说作品。"
                )
            return

        # 补全详情需要 App API 认证
        if not await self.client_wrapper.authenticate():
            yield event.plain_result(self.pixiv_config.get_auth_error_message())
            return

        illusts = []
        if data.illust_ids:
            illusts = await self._fetch_illust_details(data.illust_ids)

        novels = []
        if data.novel_ids and self.pixiv_config.collection_include_novel:
            novels = await self._fetch_novel_details(data.novel_ids)

        emitted = False
        summary_prefix = body_text

        if illusts:
            logger.info(
                f"Pixiv 插件：珍藏册 {collection_id} 补全到 {len(illusts)} 个插画作品"
            )
            async for result in self._emit_with_fallback(
                event,
                illusts,
                is_novel=False,
                display_tag_str=f"珍藏册:{data.title}",
                summary_prefix=summary_prefix,
                plain_header=None if emitted else body_text,
                recursive=recursive,
            ):
                yield result
            emitted = True

        if novels:
            logger.info(
                f"Pixiv 插件：珍藏册 {collection_id} 补全到 {len(novels)} 个小说作品"
            )
            async for result in self._emit_with_fallback(
                event,
                novels,
                is_novel=True,
                display_tag_str=f"珍藏册:{data.title}(小说)",
                summary_prefix=(
                    summary_prefix
                    if not emitted
                    else self._build_secondary_prefix(
                        data, is_novel=True, count=len(novels)
                    )
                ),
                plain_header=None if emitted else body_text,
                recursive=recursive,
            ):
                yield result
            emitted = True

        if not emitted:
            # 有作品 ID 但全部补全失败（API 限流/作品被删等）
            yield event.plain_result(
                f"{body_text}\n\n作品详情获取失败，未发送任何图片。"
            )

    async def _fetch_illust_details(self, illust_ids: List[str]) -> List[Any]:
        """并发补全插画详情，返回按珍藏册原顺序排列的 Illust 对象列表。"""
        return await self._fetch_details(
            illust_ids, api_method=self.client.illust_detail, kind="插画"
        )

    async def _fetch_novel_details(self, novel_ids: List[str]) -> List[Any]:
        """并发补全小说详情，返回按珍藏册原顺序排列的 Novel 对象列表。"""
        return await self._fetch_details(
            novel_ids, api_method=self.client.novel_detail, kind="小说"
        )

    async def _fetch_details(
        self, ids: List[str], *, api_method, kind: str
    ) -> List[Any]:
        if not ids:
            return []

        targets = ids[:MAX_FETCH_PER_COLLECTION]
        if len(ids) > len(targets):
            logger.warning(
                f"Pixiv 插件：珍藏册内{kind}数量 {len(ids)} 超过单次上限 "
                f"{MAX_FETCH_PER_COLLECTION}，仅处理前 {len(targets)} 个。"
            )

        concurrency = max(1, int(self.pixiv_config.collection_fetch_concurrency or 1))
        semaphore = asyncio.Semaphore(concurrency)
        fetched: Dict[str, Any] = {}

        async def fetch_one(work_id: str) -> None:
            async with semaphore:
                await asyncio.sleep(DETAIL_REQUEST_MIN_INTERVAL)
                try:
                    response = await self.client_wrapper.call_pixiv_api(
                        api_method, work_id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"Pixiv 插件：珍藏册获取{kind} {work_id} 详情失败 - {exc}"
                    )
                    return

                payload = None
                if response is not None:
                    payload = getattr(response, "illust", None) or getattr(
                        response, "novel", None
                    )
                if payload is not None:
                    fetched[work_id] = payload

        await asyncio.gather(*(fetch_one(work_id) for work_id in targets))

        # 保持珍藏册原有的排版顺序
        return [fetched[work_id] for work_id in targets if work_id in fetched]

    @staticmethod
    def _build_secondary_prefix(
        data: CollectionData, *, is_novel: bool, count: int
    ) -> str:
        """珍藏册被拆成多条转发时，后续几条只带一行上下文。

        第一条已经带完整信息头，后续再重复整段是噪音；但完全不提又会让
        用户在聊天里看到一条无来由的转发。折中成一行引用。
        """
        label = "小说" if is_novel else "插画"
        link = COLLECTION_PAGE_TEMPLATE.format(collection_id=data.collection_id)
        return f"📚 珍藏册《{data.title}》· {label}部分（{count} 个）\n{link}"

    async def _emit_with_fallback(
        self,
        event: AstrMessageEvent,
        works: List[Any],
        *,
        is_novel: bool,
        display_tag_str: str,
        summary_prefix: Optional[str],
        plain_header: Optional[str],
        recursive: bool = False,
    ):
        """发送一组作品；单组失败时降级为文本清单，避免整个珍藏册解析崩掉。

        小说走的是既有的小说发送链路（send_forward_message 会访问 page_count /
        meta_pages / image_urls）。已实机验证该链路对真实 Novel 对象可用
        （page_count=1、meta_pages/meta_single_page 为 None 时走封面图分支，安全），
        但珍藏册是拼贴内容、作品来源杂，仍保留兜底以免单组失败拖垮整个解析。
        """
        try:
            async for result in self._emit_works(
                event,
                works,
                is_novel=is_novel,
                display_tag_str=display_tag_str,
                summary_prefix=summary_prefix,
                plain_header=plain_header,
                recursive=recursive,
            ):
                yield result
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Pixiv 插件：珍藏册发送失败（is_novel={is_novel}）- {exc}")
            logger.error(traceback.format_exc())

        yield event.plain_result(self._build_fallback_text(works, is_novel))

    @staticmethod
    def _build_fallback_text(works: List[Any], is_novel: bool) -> str:
        """发送失败时的降级输出：只列标题与链接。"""
        label = "小说" if is_novel else "插画"
        template = (
            "https://www.pixiv.net/novel/show.php?id={work_id}"
            if is_novel
            else "https://www.pixiv.net/artworks/{work_id}"
        )
        lines = [f"⚠️ {label}图片发送失败，以下为清单（共 {len(works)} 个）："]
        for work in works[:MAX_FALLBACK_ITEMS]:
            work_id = getattr(work, "id", None)
            title = str(getattr(work, "title", "") or "无标题")
            link = template.format(work_id=work_id) if work_id is not None else ""
            lines.append(f"· {title}\n  {link}".rstrip())
        remaining = len(works) - MAX_FALLBACK_ITEMS
        if remaining > 0:
            lines.append(f"… 另有 {remaining} 个未列出")
        return "\n".join(lines)

    async def _emit_works(
        self,
        event: AstrMessageEvent,
        works: List[Any],
        *,
        is_novel: bool,
        display_tag_str: str,
        summary_prefix: Optional[str],
        plain_header: Optional[str],
        recursive: bool = False,
    ):
        """按配置过滤并发送一组作品。"""
        max_works = max(1, int(self.pixiv_config.collection_max_works or 1))

        # 递归模式行为对齐 /pixiv_specific：逐条发送每个作品的全部页面。
        # 因此强制关闭单条合并转发——几十上百张图塞进一条转发既不现实、
        # 也拿不到「按张控速」的效果。
        single_response_mode = (
            False if recursive else self.pixiv_config.single_response_mode
        )

        config = FilterConfig(
            r18_mode=self.pixiv_config.r18_mode,
            filter_r18g_only=self.pixiv_config.filter_r18g_only,
            ai_filter_mode=self.pixiv_config.ai_filter_mode,
            ai_detection_mode=self.pixiv_config.ai_detection_mode,
            display_tag_str=display_tag_str,
            return_count=max(1, min(len(works), max_works)),
            logger=logger,
            show_filter_result=self.pixiv_config.show_filter_result,
            single_response_mode=single_response_mode,
            show_details=self.pixiv_config.show_details,
            excluded_tags=[],
            forward_threshold=self.pixiv_config.forward_threshold,
            # 小说没有书签/阅读/点赞字段，沿用插件内小说链路的做法关闭统计过滤
            enable_stat_filters=not is_novel,
        )

        send_options: dict = {}
        if recursive:
            send_options = {
                "send_all_pages": True,
                "max_pages": self.pixiv_config.collection_recursive_max_pages,
                "throttle_seconds": (
                    self.pixiv_config.collection_recursive_interval_seconds
                ),
            }

        if single_response_mode:
            async for result in process_and_send_illusts_sorted(
                works,
                config,
                self.client,
                event,
                build_detail_message,
                send_pixiv_image,
                send_forward_message,
                is_novel=is_novel,
                summary_prefix=summary_prefix,
                **send_options,
            ):
                yield result
            return

        if plain_header:
            yield event.plain_result(plain_header)

        async for result in process_and_send_illusts_sorted(
            works,
            config,
            self.client,
            event,
            build_detail_message,
            send_pixiv_image,
            send_forward_message,
            is_novel=is_novel,
            **send_options,
        ):
            yield result
