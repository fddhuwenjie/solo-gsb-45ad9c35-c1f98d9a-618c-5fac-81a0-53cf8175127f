"""活动 vs 候选路由的样本归属比较编排（只读）。

流程：

1. 先固定一次活动快照（读同一引用，比较期间的任何发布都不影响本次结果）；
2. 对候选运行与 /publish 完全相同的校验（``evaluate_candidates``）：
   有解析错误、导出路径重复或 full_shadow 即拒绝，返回原始下标与冲突，
   整批不做归属比较；
3. 编译两侧路由，逐样本做同构的解码/规范化与唯一命中选择；
4. 归属身份按「方法 + 规范化模板 + 显式 id」判定，仅下标移动不算改投。

预检不修改活动配置；返回的 version 即本次固定快照的版本。
"""

from __future__ import annotations

from .matching import (
    CompiledRoute,
    SampleError as SampleValidationError,
    attribution_identity,
    compile_routes,
    normalize_request,
    select_match,
)
from .models import (
    CompareRequest,
    CompareResponse,
    RouteMatch,
    SampleError,
    SampleResult,
)
from .normalization import render
from .publish import PublishStore, evaluate_candidates


def _match_view(match: CompiledRoute | None):
    if match is None:
        return RouteMatch(index=None, id=None, normalized_template=None)
    return RouteMatch(
        index=match.index,
        id=match.effective_id,
        normalized_template=match.parsed.normalized,
    )


def compare_attribution(
    store: PublishStore, request: CompareRequest
) -> CompareResponse:
    # 先固定活动快照；整个比较基于同一版本，预检不产生任何写入。
    snapshot = store.get()
    version = snapshot.version

    evaluation = evaluate_candidates(request.routes)
    if not evaluation.accepted:
        return CompareResponse(
            status="rejected",
            version=version,
            parse_errors=evaluation.parse_errors,
            conflicts=evaluation.full_shadows,
            results=None,
        )

    active_routes, _active_errors = compile_routes(snapshot.routes)
    candidate_routes, _candidate_errors = compile_routes(request.routes)

    results: list[SampleResult] = []
    for index, sample in enumerate(request.samples):
        try:
            method, segments = normalize_request(sample.method, sample.path)
        except SampleValidationError as exc:
            results.append(
                SampleResult(
                    index=index,
                    method=sample.method,
                    path=sample.path,
                    normalized_path=None,
                    active=None,
                    candidate=None,
                    rerouted=None,
                    errors=[SampleError(code=exc.code, message=str(exc))],
                )
            )
            continue

        active_match = select_match(active_routes, method, segments)
        candidate_match = select_match(candidate_routes, method, segments)
        rerouted = attribution_identity(
            active_match, method
        ) != attribution_identity(candidate_match, method)

        results.append(
            SampleResult(
                index=index,
                method=method,
                path=sample.path,
                normalized_path=render(segments),
                active=_match_view(active_match),
                candidate=_match_view(candidate_match),
                rerouted=rerouted,
                errors=[],
            )
        )

    return CompareResponse(
        status="compared",
        version=version,
        parse_errors=[],
        conflicts=[],
        results=results,
    )
