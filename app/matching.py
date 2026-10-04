"""样本请求在活动与候选路由下的归属比较（只读预检）。

候选路由必须通过与 ``/publish`` 完全相同的校验（审计解析错误、OpenAPI 导出
路径重复、``full_shadow``）；一旦候选被拒，整批不做归属比较。

两侧路由均按**路由器的真实决胜规则**为每个样本选出唯一命中：

1. 优先级数值高者胜；
2. 优先级相同则注册更早者（同方法内的注册序号）胜。

归属身份按「方法 + 规范化模板 + 显式 id」判定——仅原始下标移动不算改投。

请求样本的方法或绝对路径非法时逐项报错（``invalid_method`` /
``invalid_path``），其余样本继续；请求路径走与审计完全相同的解码与规范化，
因此 ``%2F`` 不会变成段分隔符。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import analyzer
from .models import (
    ALLOWED_METHODS,
    ParseError,
    RouteIn,
)
from .normalization import PathError, normalize_path
from .templates import TemplateError, parse_template


class SampleError(ValueError):
    """样本请求非法；``code`` 为 invalid_method / invalid_path。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class CompiledRoute:
    """可用于样本匹配的已解析路由。"""

    index: int  # 在输入数组中的原始下标
    method: str
    priority: int
    parsed: Any  # templates.ParsedTemplate
    order_in_method: int  # 同方法内注册序号（从 0 起）
    explicit_id: Optional[str]  # 调用方显式提供的 id（未提供为 None）
    effective_id: str  # 与审计一致的生效 id：显式 id 或 route-<下标>

    def as_route(self) -> analyzer.Route:
        return analyzer.Route(
            index=self.index,
            route_id=self.effective_id,
            method=self.method,
            priority=self.priority,
            parsed=self.parsed,
            order_in_method=self.order_in_method,
        )


def _as_route_in(item: RouteIn | dict[str, Any]) -> RouteIn:
    if isinstance(item, RouteIn):
        return item
    return RouteIn.model_validate(item)


def compile_routes(
    items: list[RouteIn] | list[dict[str, Any]],
) -> tuple[list[CompiledRoute], list[ParseError]]:
    """按与 service 相同的规则解析路由（方法检查 + 模板解析）。

    返回有效编译路由与逐项解析错误；活动快照理论上只含有效路由。
    """
    compiled: list[CompiledRoute] = []
    parse_errors: list[ParseError] = []
    method_counters: dict[str, int] = {}

    for index, raw in enumerate(items):
        item = _as_route_in(raw)
        method = item.method.strip().upper()
        if method not in ALLOWED_METHODS:
            parse_errors.append(
                ParseError(
                    index=index,
                    id=item.id,
                    method=item.method,
                    template=item.path,
                    error=f"unsupported HTTP method {item.method!r}",
                )
            )
            continue
        try:
            parsed = parse_template(item.path)
        except TemplateError as exc:
            parse_errors.append(
                ParseError(
                    index=index,
                    id=item.id,
                    method=method,
                    template=item.path,
                    error=str(exc),
                )
            )
            continue

        order = method_counters.get(method, 0)
        method_counters[method] = order + 1
        explicit_id = item.id or None
        compiled.append(
            CompiledRoute(
                index=index,
                method=method,
                priority=item.priority,
                parsed=parsed,
                order_in_method=order,
                explicit_id=explicit_id,
                effective_id=item.id if item.id else f"route-{index}",
            )
        )

    return compiled, parse_errors


def select_match(
    routes: list[CompiledRoute], method: str, segments: list[str]
) -> CompiledRoute | None:
    """按优先级降序、同级注册先后选出唯一命中路由；无命中返回 None。"""
    winner: CompiledRoute | None = None
    for route in routes:
        if route.method != method:
            continue
        if not analyzer.matches(route.as_route(), segments):
            continue
        # 优先级高者胜；同级时注册更早者（order_in_method 更小）胜。
        if winner is None or (
            route.priority > winner.priority
            or (
                route.priority == winner.priority
                and route.order_in_method < winner.order_in_method
            )
        ):
            winner = route
    return winner


def normalize_request(method: str, path: str) -> tuple[str, list[str]]:
    """校验样本方法与绝对路径，并规范化为段列表。

    非法时抛出 ValueError（消息即为逐项错误信息）。
    """
    normalized_method = method.strip().upper()
    if normalized_method not in ALLOWED_METHODS:
        raise SampleError(
            "invalid_method", f"unsupported HTTP method {method!r}"
        )
    if not isinstance(path, str) or not path.startswith("/"):
        raise SampleError(
            "invalid_path",
            "request path must be an absolute path starting with '/'",
        )
    try:
        segments = normalize_path(path)
    except PathError as exc:
        raise SampleError("invalid_path", str(exc)) from exc
    return normalized_method, segments


def attribution_identity(match: CompiledRoute | None, method: str) -> tuple | None:
    """归属身份：方法 + 规范化模板 + 显式 id；无命中为 None。"""
    if match is None:
        return None
    return (method, match.parsed.normalized, match.explicit_id)
