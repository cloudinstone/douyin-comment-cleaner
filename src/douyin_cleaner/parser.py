"""UI hierarchy XML parser for Douyin outbound comment management."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

TAB_LABELS = frozenset(
    {
        "全部",
        "发出的评论",
        "收到的评论",
        "赞和收藏",
        "粉丝",
        "推荐",
        "朋友",
        "消息",
        "我",
        "首页",
        "商城",
        "同城",
    }
)
OTHER_FILTER_LABELS = frozenset(
    {"全部", "全部消息", "收到的评论", "赞和收藏", "赞", "艾特我的", "@我的"}
)

DELETE_LABELS = ("删除", "删除评论")
CONFIRM_LABELS = ("确认删除", "确认", "确定")
POPUP_ACTION_LABELS = frozenset(
    {"删除", "删除评论", "复制", "举报", "回复", "置顶", "屏蔽", "转发"}
)
RATE_LIMIT_KEYWORDS = (
    "操作太频繁",
    "操作过于频繁",
    "请完成验证",
    "安全验证",
    "稍后再试",
    "拖动滑块",
    "验证身份",
)
END_OF_LIST_KEYWORDS = (
    "暂时没有更多了",
    "没有更多了",
    "暂无评论",
    "还没有发过评论",
)


@dataclass(frozen=True)
class Bounds:
    """Screen bounding box in pixel coordinates."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, other: Bounds) -> bool:
        """Return True if this box completely encloses `other`."""
        return (
            self.left <= other.left
            and self.top <= other.top
            and self.right >= other.right
            and self.bottom >= other.bottom
        )

    def vertical_overlap_ratio(self, other: Bounds) -> float:
        """Return overlap height divided by the smaller box height."""
        overlap = max(0, min(self.bottom, other.bottom) - max(self.top, other.top))
        min_h = min(self.height, other.height)
        if min_h <= 0:
            return 0.0
        return overlap / min_h

    @classmethod
    def from_str(cls, bounds_str: str) -> Bounds | None:
        """Parse an Android uiautomator bounds string like '[0,233][1080,480]'."""
        match = _BOUNDS_RE.fullmatch(bounds_str.strip())
        if not match:
            return None
        left, top, right, bottom = (int(g) for g in match.groups())
        if right <= left or bottom <= top:
            return None
        return cls(left=left, top=top, right=right, bottom=bottom)

    def format(self) -> str:
        """Format back to '[l,t][r,b]'."""
        return f"[{self.left},{self.top}][{self.right},{self.bottom}]"


@dataclass
class UINode:
    """Normalized node from an Android uiautomator XML dump."""

    text: str
    content_desc: str
    resource_id: str
    class_name: str
    package: str
    clickable: bool
    long_clickable: bool
    selected: bool
    bounds: Bounds
    children: list[UINode] = field(default_factory=list)
    parent: UINode | None = field(default=None, repr=False)

    def collect_texts(self) -> list[str]:
        """Collect all distinct non-empty text and content-desc values in subtree."""
        seen: set[str] = set()
        result: list[str] = []

        def _walk(node: UINode) -> None:
            for val in (node.text.strip(), node.content_desc.strip()):
                if val and val not in seen:
                    seen.add(val)
                    result.append(val)
            for child in node.children:
                _walk(child)

        _walk(self)
        return result


@dataclass(frozen=True)
class CommentRow:
    """Detected comment list row ready for inspection or long-press deletion."""

    bounds: Bounds
    tap_point: tuple[int, int]
    texts: list[str]
    summary: str
    fingerprint: str


@dataclass
class ScreenAnalysis:
    """Structured interpretation of the current screen UI hierarchy."""

    package: str
    is_lockscreen: bool
    rate_limit_or_captcha: str | None
    delete_button: Bounds | None
    confirm_button: Bounds | None
    has_popup_menu: bool
    subtab_sent_comments: UINode | None
    subtab_sent_selected: bool | None
    comment_rows: list[CommentRow]
    reached_end: bool


def _parse_bool(raw: str | None) -> bool:
    return raw == "true"


def parse_hierarchy(xml_str: str) -> list[UINode]:
    """Parse XML hierarchy string into a list of root UINode trees."""
    root_el = ET.fromstring(xml_str)
    roots: list[UINode] = []

    def _build(el: ET.Element, parent: UINode | None) -> list[UINode]:
        bounds = Bounds.from_str(el.attrib.get("bounds", ""))
        if bounds is None:
            built: list[UINode] = []
            for child_el in el:
                built.extend(_build(child_el, parent))
            return built

        node = UINode(
            text=el.attrib.get("text", ""),
            content_desc=el.attrib.get("content-desc", ""),
            resource_id=el.attrib.get("resource-id", ""),
            class_name=el.attrib.get("class", ""),
            package=el.attrib.get("package", ""),
            clickable=_parse_bool(el.attrib.get("clickable")),
            long_clickable=_parse_bool(el.attrib.get("long-clickable")),
            selected=_parse_bool(el.attrib.get("selected")),
            bounds=bounds,
            parent=parent,
        )
        for child_el in el:
            node.children.extend(_build(child_el, node))
        return [node]

    for child in root_el:
        roots.extend(_build(child, None))
    return roots


def _iter_nodes(roots: list[UINode]) -> list[UINode]:
    all_nodes: list[UINode] = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        all_nodes.append(node)
        if node.children:
            stack.extend(reversed(node.children))
    return all_nodes


def _dominant_package(nodes: list[UINode]) -> str:
    counts = Counter(n.package for n in nodes if n.package)
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def _detect_lockscreen(package: str, nodes: list[UINode]) -> bool:
    if package != "com.android.systemui":
        return False
    for node in nodes:
        rid = node.resource_id
        if rid.startswith("com.android.systemui:id/keyguard_") or rid in {
            "com.android.systemui:id/device_entry_icon_view",
            "com.android.systemui:id/lock_icon",
        }:
            return True
    return False


def _node_label(node: UINode) -> str:
    return (node.text or node.content_desc).strip()


def _button_target_bounds(node: UINode, screen_height: int) -> Bounds:
    """Prefer clickable ancestor bounds when reasonably button-sized."""
    cur: UINode | None = node
    while cur is not None:
        if cur.clickable and cur.bounds.height <= int(screen_height * 0.16):
            return cur.bounds
        cur = cur.parent
    return node.bounds


def _is_comment_text_widget(node: UINode) -> bool:
    """Return True if node looks like comment body text rather than a dialog/menu button."""
    if node.long_clickable:
        return True
    if "MentionTextView" in node.class_name:
        return True
    return False


def _find_action_button(
    nodes: list[UINode], labels: tuple[str, ...], screen_height: int
) -> Bounds | None:
    for label in labels:
        for node in nodes:
            if _is_comment_text_widget(node):
                continue
            if _node_label(node) == label and node.bounds.height <= int(
                screen_height * 0.15
            ):
                return _button_target_bounds(node, screen_height)
    return None


def _detect_popup_menu(nodes: list[UINode], screen_height: int) -> bool:
    action_hits = 0
    for node in nodes:
        if _is_comment_text_widget(node):
            continue
        label = _node_label(node)
        if label in POPUP_ACTION_LABELS and node.bounds.height <= int(
            screen_height * 0.12
        ):
            if label in DELETE_LABELS:
                return True
            action_hits += 1
    return action_hits >= 2


def _find_sent_subtab(
    nodes: list[UINode], screen_height: int = 2340
) -> tuple[UINode | None, bool | None]:
    for node in nodes:
        if _is_comment_text_widget(node):
            continue
        if _node_label(node) == "发出的评论":
            if node.selected or (
                node.parent is not None and node.parent.selected
            ):
                return node, True
            is_tab_widget = "Tab" in node.class_name or (
                node.parent is not None and "Tab" in node.parent.class_name
            )
            has_sibling_filter = any(
                _node_label(n) in OTHER_FILTER_LABELS
                and n.bounds.top < int(screen_height * 0.35)
                for n in nodes
            )
            if (
                not is_tab_widget
                and not has_sibling_filter
                and node.bounds.top < int(screen_height * 0.20)
            ):
                return node, True
            return node, False
    return None, None


def _is_tab_node(node: UINode) -> bool:
    texts = node.collect_texts()
    if len(texts) == 1 and texts[0] in TAB_LABELS:
        return True
    if "Tab" in node.class_name:
        return True
    return False


def _promote_to_row_container(
    node: UINode, screen_width: int, screen_height: int
) -> UINode:
    """Walk up to a row-wide container if the long-clickable node is an inner view."""
    if node.bounds.width >= int(screen_width * 0.75):
        return node
    cur = node.parent
    while cur is not None:
        h = cur.bounds.height
        w = cur.bounds.width
        if w >= int(screen_width * 0.75) and h <= int(screen_height * 0.45):
            # Avoid promoting all the way to the RecyclerView itself
            if "RecyclerView" in cur.class_name or "ListView" in cur.class_name:
                break
            return cur
        cur = cur.parent
    return node


def _extract_comment_rows(
    nodes: list[UINode], screen_width: int, screen_height: int
) -> list[CommentRow]:
    min_top = int(screen_height * 0.10)
    max_bottom = int(screen_height * 0.94)
    min_center_y = int(screen_height * 0.12)
    max_center_y = int(screen_height * 0.92)
    min_height = int(screen_height * 0.035)
    max_height = int(screen_height * 0.45)

    raw_candidates: list[UINode] = []
    for node in nodes:
        if not node.long_clickable:
            continue
        b = node.bounds
        cy = b.center[1]
        if (
            b.top < min_top
            or b.bottom > max_bottom
            or cy < min_center_y
            or cy > max_center_y
            or b.height < min_height
            or b.height > max_height
        ):
            continue
        if _is_tab_node(node):
            continue
        raw_candidates.append(node)

    # Prefer outer row containers over nested long-clickable child text views
    wide_containers = [
        n for n in raw_candidates if n.bounds.width >= int(screen_width * 0.75)
    ]
    selected_nodes: list[UINode] = list(wide_containers)

    for node in raw_candidates:
        if node.bounds.width >= int(screen_width * 0.75):
            continue
        if any(outer.bounds.contains(node.bounds) for outer in wide_containers):
            continue
        promoted = _promote_to_row_container(node, screen_width, screen_height)
        if (
            promoted.bounds.top >= min_top
            and promoted.bounds.bottom <= max_bottom
            and not _is_tab_node(promoted)
        ):
            selected_nodes.append(promoted)

    selected_nodes.sort(key=lambda n: (n.bounds.top, n.bounds.left))

    # Deduplicate overlapping containers
    deduped: list[UINode] = []
    for node in selected_nodes:
        if any(
            existing.bounds.vertical_overlap_ratio(node.bounds) > 0.5
            for existing in deduped
        ):
            continue
        deduped.append(node)

    rows: list[CommentRow] = []
    safe_tap_x = int(screen_width * 0.50)
    for idx, node in enumerate(deduped):
        texts = node.collect_texts()
        if not texts:
            fingerprint = f"__empty_row_{idx}_{node.bounds.height}"
            summary = "<empty>"
        else:
            normalized = [re.sub(r"\s+", " ", t).strip() for t in texts]
            fingerprint = "|".join(normalized)
            summary = " · ".join(normalized)[:140]

        tap_y = node.bounds.center[1]
        rows.append(
            CommentRow(
                bounds=node.bounds,
                tap_point=(safe_tap_x, tap_y),
                texts=texts,
                summary=summary,
                fingerprint=fingerprint,
            )
        )

    return rows


def analyze_screen(
    xml_str: str, screen_width: int = 1080, screen_height: int = 2340
) -> ScreenAnalysis:
    """Analyze a uiautomator XML dump and extract comment-cleaner screen state."""
    roots = parse_hierarchy(xml_str)
    nodes = _iter_nodes(roots)

    package = _dominant_package(nodes)
    is_lockscreen = _detect_lockscreen(package, nodes)

    rate_limit_msg: str | None = None
    reached_end = False
    for node in nodes:
        label = _node_label(node)
        if not label:
            continue
        if rate_limit_msg is None:
            for kw in RATE_LIMIT_KEYWORDS:
                if kw in label:
                    rate_limit_msg = label
                    break
        if not reached_end:
            for kw in END_OF_LIST_KEYWORDS:
                if kw in label:
                    reached_end = True
                    break

    delete_button = _find_action_button(nodes, DELETE_LABELS, screen_height)
    confirm_button = _find_action_button(nodes, CONFIRM_LABELS, screen_height)
    has_popup_menu = _detect_popup_menu(nodes, screen_height)
    subtab_node, subtab_selected = _find_sent_subtab(nodes, screen_height)

    comment_rows: list[CommentRow] = []
    if not is_lockscreen and delete_button is None and confirm_button is None:
        comment_rows = _extract_comment_rows(nodes, screen_width, screen_height)

    return ScreenAnalysis(
        package=package,
        is_lockscreen=is_lockscreen,
        rate_limit_or_captcha=rate_limit_msg,
        delete_button=delete_button,
        confirm_button=confirm_button,
        has_popup_menu=has_popup_menu,
        subtab_sent_comments=subtab_node,
        subtab_sent_selected=subtab_selected,
        comment_rows=comment_rows,
        reached_end=reached_end,
    )
