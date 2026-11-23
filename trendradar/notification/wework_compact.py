# coding=utf-8
"""
企业微信紧凑版推送渲染

设计目标（与上游 splitter 并存，互不影响）：
- 按「平台」出榜单，每行带该平台内的真实热搜名次
- 新增条目用文字标记 [新]（不使用 emoji，避免客户端渲染成图片按钮）
- 去掉每行重复的时间戳与无意义缩进，抬头只在第一页出现一次
- 按字节上限分页，只在条目边界断开，不把标题劈成两半

本模块只依赖标准库，不导入 splitter / renderer，避免影响其它推送渠道。
"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 新增条目的文字标记（替代原来的 emoji 🆕）
NEW_MARK = "[新]"

# 单个平台最多展示条数（0 = 不限制）
DEFAULT_MAX_PER_PLATFORM = 5

# 热词摘要最多展示个数
DEFAULT_MAX_KEYWORDS = 8


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
    ranks = _as_int_list(title_data.get("ranks"))
    if not ranks:
        return "·"
    low, high = min(ranks), max(ranks)
    return f"#{low}" if low == high else f"#{low}-{high}"


def _item_line(title_data: Dict, show_rank: bool = True) -> str:
    """单个条目的渲染：名次 + 标题链接 + [新]"""
    title = _clean(title_data.get("title"))
    url = _clean(title_data.get("mobile_url") or title_data.get("url") or "")
    body = f"[{title}]({url})" if (title and url) else title
    if not body:
        return ""

    parts: List[str] = []
    if show_rank:
        parts.append(_rank_prefix(title_data))
    parts.append(body)
    if title_data.get("is_new"):
        parts.append(NEW_MARK)
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
        ranks = _as_int_list(item.get("ranks"))
        return (min(ranks) if ranks else 999, -int(item.get("count") or 0))

    return sorted(items, key=sort_key)


def _group_by_platform(stats: List[Dict], max_per_platform: int) -> List[Tuple[str, List[Dict]]]:
    """把关键词分组的数据重新按平台聚合（数据源与关键词模式完全相同）"""
    groups: Dict[str, List[Dict]] = {}
    for stat in stats:
        for item in stat.get("titles") or []:
            name = _clean(item.get("source_name")) or "其它"
            groups.setdefault(name, []).append(item)

    result: List[Tuple[str, List[Dict]]] = []
    for name, items in groups.items():
        items = _sort_by_rank(_dedupe(items))
        if max_per_platform and max_per_platform > 0:
            items = items[:max_per_platform]
        if items:
            result.append((name, items))

    # 命中条数多的平台排前面，同数量按平台名排，保证顺序稳定
    result.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    return result


def _keyword_summary(stats: List[Dict], max_keywords: int) -> str:
    """热词摘要：词 + 条数，一行装下"""
    words: List[Tuple[str, int]] = []
    for stat in stats:
        word = _clean(stat.get("word"))
        if not word:
            continue
        count = int(stat.get("count") or len(stat.get("titles") or []) or 0)
        words.append((word, count))
    words.sort(key=lambda pair: (-pair[1], pair[0]))
    if max_keywords and max_keywords > 0:
        words = words[:max_keywords]
    return " · ".join(f"{word} {count}" for word, count in words)


def _build_blocks(
    stats: List[Dict], max_per_platform: int, max_keywords: int
) -> List[str]:
    """构建分页前的区块列表，每个区块内部不拆断"""
    blocks: List[str] = []

    for name, items in _group_by_platform(stats, max_per_platform):
        lines = [f"**{name}**"] + [_item_line(item) for item in items]
        block = "\n".join(line for line in lines if line)
        if block:
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
    reserve = 24  # 为「第 n/m 页」标记预留
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
        result.append(f"{page_header}\n\n{body}{footer}" if footer else f"{page_header}\n\n{body}")
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

    bits = [now.strftime("%m-%d %H:%M"), f"{total_count} 条"]
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
            f"有新版本 {update_info.get('remote_version', '')}（当前 {update_info.get('current_version', '')}）"
        )
    footer = ("\n\n" + "\n".join(footer_lines)) if footer_lines else ""

    blocks = _build_blocks(stats, max_per_platform, max_keywords)

    if ai_content and ai_content.strip():
        # AI 分析是多行文本，原样保留换行，不做单行压缩
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
