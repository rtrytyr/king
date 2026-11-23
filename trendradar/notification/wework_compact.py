# coding=utf-8
"""
企业微信紧凑版推送渲染

设计目标（与上游 splitter 并存，互不影响）：
- 优先使用平台真实完整榜单（前 N 条，不受关键词过滤影响）
- 每行带该平台内的真实热搜名次
- 新上榜条目标 emoji（默认 ✨），不使用会被渲染成图片的 🆕
- 平台名前加该平台图标
- 去掉每行重复的时间戳与多余缩进，抬头只出现在第一页
- 按字节上限分页，只在条目边界断开，不把标题劈成两半

本模块只依赖标准库，不导入 splitter / renderer，避免影响其它推送渠道。
"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 新上榜标记（可在 config 的 wework.new_emoji 里换）
DEFAULT_NEW_EMOJI = "✨"

# 单个平台最多展示条数（0 = 不限制）
DEFAULT_MAX_PER_PLATFORM = 10

# 热词摘要最多展示个数
DEFAULT_MAX_KEYWORDS = 8

# 平台图标（企业微信 markdown 放不了真 logo，用 emoji 代替）
PLATFORM_EMOJI: Dict[str, str] = {
    "微博": "🔥",
    "微博热搜": "🔥",
    "bilibili": "📺",
    "bilibili 热搜": "📺",
    "哔哩哔哩": "📺",
    "抖音": "🎵",
    "快手": "🎬",
    "知乎": "💡",
    "贴吧": "💬",
    "百度热搜": "🔍",
    "百度": "🔍",
    "今日头条": "📰",
    "头条": "📰",
    "华尔街见闻": "💹",
    "财联社热门": "📈",
    "财联社": "📈",
    "澎湃新闻": "🌊",
    "凤凰网": "🦅",
    "36氪": "🚀",
    "虎扑": "🏀",
    "IT之家": "💻",
    "少数派": "🧩",
    "雪球": "📉",
    "掘金": "⛏️",
}
DEFAULT_PLATFORM_EMOJI = "📌"

# 热词里过滤掉的国名/地区名（只留主语、名词）
COUNTRY_WORDS = {
    "中国", "美国", "日本", "韩国", "英国", "法国", "德国", "俄罗斯", "印度",
    "欧洲", "北美", "亚洲", "非洲", "中东", "东南亚", "乌克兰", "以色列",
    "伊朗", "朝鲜", "越南", "泰国", "菲律宾", "澳大利亚", "加拿大", "巴西",
    "台湾", "香港", "澳门", "全国", "国内", "国外", "海外",
}


def _clean(text: Any) -> str:
    """压缩空白、去掉换行，避免标题把排版撑乱"""
    if text is None:
        return ""
    s = str(text).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return " ".join(s.split()).strip()


def _as_int_list(values: Any) -> List[int]:
    result: List[int] = []
    for v in values or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            result.append(n)
    return result


def _rank_prefix(title_data: Dict) -> str:
    """把 rank 列表渲染成紧凑名次前缀：#3 / #3-5 / ·"""
    ranks = _as_int_list(title_data.get("ranks") or [title_data.get("rank")])
    if not ranks:
        return "·"
    low, high = min(ranks), max(ranks)
    return f"#{low}" if low == high else f"#{low}-{high}"


def _item_line(title_data: Dict, new_emoji: str = DEFAULT_NEW_EMOJI) -> str:
    """单个条目的渲染：名次 + 标题链接 + 新榜标记"""
    title = _clean(title_data.get("title"))
    # 标题里的方括号会把 markdown 链接解析搞乱，替换成全角
    title = title.replace("[", "［").replace("]", "］")
    url = _clean(
        title_data.get("mobile_url")
        or title_data.get("mobileUrl")
        or title_data.get("url")
        or ""
    )
    body = f"[{title}]({url})" if (title and url) else title
    if not body:
        return ""

    parts: List[str] = [_rank_prefix(title_data)]
    parts.append(body)
    if title_data.get("is_new"):
        parts.append(new_emoji)
    return " ".join(p for p in parts if p)


def _dedupe(items: Iterable[Dict]) -> List[Dict]:
    """同一平台内按标题去重，保留首次出现的记录"""
    seen = set()
    unique: List[Dict] = []
    for item in items:
        key = _clean(item.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _sort_by_rank(items: List[Dict]) -> List[Dict]:
    def sort_key(item: Dict) -> Tuple[int, int]:
        ranks = _as_int_list(item.get("ranks") or [item.get("rank")])
        return (min(ranks) if ranks else 999, -int(item.get("count") or 0))

    return sorted(items, key=sort_key)


def _new_title_set(report_data: Dict) -> set:
    """收集本次新增的标题集合，供榜单标注新榜条目"""
    titles = set()
    for source in report_data.get("new_titles") or []:
        rows = source.get("titles") if isinstance(source, dict) else None
        for row in rows or []:
            if isinstance(row, dict):
                name = _clean(row.get("title"))
            else:
                name = _clean(row)
            if name:
                titles.add(name)
    return titles


def _sections_from_platform_top(
    platform_top: Dict[str, List[Dict]],
    new_titles: set,
    max_per_platform: int,
    new_emoji: str,
) -> List[Tuple[str, List[str]]]:
    sections: List[Tuple[str, List[str]]] = []
    for name, rows in platform_top.items():
        items = []
        for row in rows or []:
            title = _clean(row.get("title"))
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "url": row.get("url", ""),
                    "mobile_url": row.get("mobile_url") or row.get("mobileUrl", ""),
                    "ranks": row.get("ranks") or [row.get("rank")],
                    "is_new": bool(row.get("is_new")) or (title in new_titles),
                }
            )
        items = _sort_by_rank(_dedupe(items))
        if max_per_platform and max_per_platform > 0:
            items = items[:max_per_platform]
        if items:
            sections.append((name, [_item_line(i, new_emoji) for i in items]))

    sections.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    return sections


def _sections_from_stats(
    stats: List[Dict], max_per_platform: int, new_emoji: str
) -> List[Tuple[str, List[str]]]:
    """没有完整榜单时的回退：把关键词分组的数据按平台重新聚合"""
    groups: Dict[str, List[Dict]] = {}
    for stat in stats:
        for item in stat.get("titles") or []:
            name = _clean(item.get("source_name")) or "其它"
            groups.setdefault(name, []).append(item)

    sections: List[Tuple[str, List[str]]] = []
    for name, items in groups.items():
        items = _sort_by_rank(_dedupe(items))
        if max_per_platform and max_per_platform > 0:
            items = items[:max_per_platform]
        if items:
            sections.append((name, [_item_line(i, new_emoji) for i in items]))

    sections.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    return sections


def _keyword_summary(stats: List[Dict], max_keywords: int) -> str:
    """热词摘要：过滤国名，词标红（企业微信支持 font color=warning）"""
    words: List[Tuple[str, int]] = []
    for stat in stats:
        word = _clean(stat.get("word"))
        if not word or word in COUNTRY_WORDS:
            continue
        count = int(stat.get("count") or len(stat.get("titles") or []) or 0)
        words.append((word, count))
    words.sort(key=lambda pair: (-pair[1], pair[0]))
    if max_keywords and max_keywords > 0:
        words = words[:max_keywords]
    return " · ".join(
        f'<font color="warning">{word}</font> {count}' for word, count in words
    )


def _platform_title(name: str) -> str:
    emoji = PLATFORM_EMOJI.get(name, DEFAULT_PLATFORM_EMOJI)
    return f"{emoji} **{name}**"


def _build_blocks(
    report_data: Dict,
    stats: List[Dict],
    max_per_platform: int,
    max_keywords: int,
    new_emoji: str,
) -> List[str]:
    """构建分页前的区块列表，每个区块内部不拆断"""
    blocks: List[str] = []

    platform_top = report_data.get("platform_top")
    if isinstance(platform_top, dict) and platform_top:
        sections = _sections_from_platform_top(
            platform_top, _new_title_set(report_data), max_per_platform, new_emoji
        )
    else:
        sections = _sections_from_stats(stats, max_per_platform, new_emoji)

    for name, lines in sections:
        block = "\n".join([_platform_title(name)] + [line for line in lines if line])
        if block.strip():
            blocks.append(block)

    summary = _keyword_summary(stats, max_keywords)
    if summary:
        blocks.append(f"**热词**\n{summary}")

    return blocks


def _split_oversized(block: str, limit: int) -> List[str]:
    """单块超过单页上限时，按行切开（不会切断单行）"""
    if len(block.encode("utf-8")) <= limit:
        return [block]

    parts: List[str] = []
    current = ""
    for line in block.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode("utf-8")) > limit and current:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _paginate(blocks: List[str], header: str, footer: str, batch_size: int) -> List[str]:
    """把区块装进若干页，每页都带抬头与页脚"""
    header_bytes = len(header.encode("utf-8"))
    footer_bytes = len(footer.encode("utf-8"))
    reserve = 24  # 为「续 n/m」标记预留
    limit = max(400, batch_size - header_bytes - footer_bytes - reserve)

    pages: List[str] = []
    current = ""

    for block in blocks:
        for piece in _split_oversized(block, limit):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate.encode("utf-8")) > limit and current:
                pages.append(current)
                current = piece
            else:
                current = candidate

    if current:
        pages.append(current)

    if not pages:
        return []

    total = len(pages)
    result: List[str] = []
    for index, body in enumerate(pages, 1):
        page_header = header if index == 1 else f"{header}（续 {index}/{total}）"
        result.append(
            f"{page_header}\n\n{body}{footer}" if footer else f"{page_header}\n\n{body}"
        )
    return result


def render_wework_compact(
    report_data: Dict,
    report_type: str = "",
    mode: str = "daily",
    rss_items: Optional[List[Dict]] = None,
    ai_content: Optional[str] = None,
    update_info: Optional[Dict] = None,
    batch_size: int = 4000,
    max_per_platform: int = DEFAULT_MAX_PER_PLATFORM,
    max_keywords: int = DEFAULT_MAX_KEYWORDS,
    new_emoji: str = DEFAULT_NEW_EMOJI,
    now: Optional[datetime] = None,
) -> List[str]:
    """渲染企业微信紧凑版消息，返回按字节上限切好的消息列表"""
    now = now or datetime.now()
    stats = report_data.get("stats") or []

    total_count = sum(len(stat.get("titles") or []) for stat in stats)
    new_count = int(report_data.get("total_new_count") or 0)
    failed_ids = report_data.get("failed_ids") or []
    platform_total = int(report_data.get("platform_total") or 0)
    platform_ok = platform_total - len(failed_ids) if platform_total else 0

    bits = [now.strftime("%m-%d %H:%M")]
    platform_top = report_data.get("platform_top")
    if isinstance(platform_top, dict) and platform_top:
        limit = max_per_platform or DEFAULT_MAX_PER_PLATFORM
        shown = sum(min(len(rows or []), limit) for rows in platform_top.values())
        bits.append(f"{shown} 条")
    else:
        bits.append(f"{total_count} 条")
    if new_count:
        bits.append(f"新 {new_count}")
    if platform_total:
        bits.append(f"平台 {platform_ok}/{platform_total}")
    header = " · ".join(bits)

    footer_lines: List[str] = []
    if failed_ids:
        footer_lines.append("抓取失败：" + "、".join(_clean(x) for x in failed_ids[:5]))
    if update_info:
        footer_lines.append(
            f"有新版本 {update_info.get('remote_version', '')}"
            f"（当前 {update_info.get('current_version', '')}）"
        )
    footer = ("\n\n" + "\n".join(footer_lines)) if footer_lines else ""

    blocks = _build_blocks(report_data, stats, max_per_platform, max_keywords, new_emoji)

    if ai_content and ai_content.strip():
        # AI 分析是多行文本，原样保留换行
        blocks.append(ai_content.strip())

    if rss_items:
        rss_lines = ["**RSS**"]
        for item in rss_items[:10]:
            title = _clean(item.get("title"))
            url = _clean(item.get("url"))
            if not title:
                continue
            rss_lines.append(f"[{title}]({url})" if url else title)
        if len(rss_lines) > 1:
            blocks.append("\n".join(rss_lines))

    pages = _paginate(blocks, header, footer, batch_size)
    if not pages:
        empty = "增量模式下暂无新增匹配的热点" if mode == "incremental" else "暂无匹配的热点"
        pages = [f"{header}\n\n{empty}{footer}"]
    return pages
