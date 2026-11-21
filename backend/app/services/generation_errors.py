"""Public generation diagnostics independent of provider exception text."""

from typing import Literal

from app.services.context_errors import RequiredContextUnavailable

from app.services.production.control import PipelineConflict
from app.services.production.fencing import StaleChapterWrite


def cognition_unavailable_message(error: Exception) -> str:
    reason = str(error)
    if reason in {'character_cognition_retrieval_unavailable', 'character_cognition_embedding_changed'}:
        return '人物认知检索暂不可用，请稍后重试或检查嵌入与检索服务配置'
    if reason in {'character_cognition_decision_changed', 'character_cognition_snapshot_unavailable'}:
        return '人物认知或前文已变化，请刷新后重新准备上下文'
    if reason in {'character_cognition_retrieval_pending', 'character_cognition_transaction_open'}:
        return '人物认知上下文尚未准备完成，请重试；若持续出现，请检查生成服务'
    if reason == 'character_cognition_context_budget_exceeded':
        return '本章必需的认知记录超过上下文容量，请调整上下文预算后重试'
    return '人物认知材料暂不可用，请到“人物认知”查看提取状态和原文依据，补提取缺失章节或重试失败项后再操作'


def review_context_message(error: Exception) -> str:
    if isinstance(error, RequiredContextUnavailable) and str(error).startswith('character_cognition_'):
        return cognition_unavailable_message(error)
    return '评审所需资料暂不可用，请稍后重试或检查章节及资料服务'


def generation_error_payload(
    error: Exception, operation: Literal['chapter', 'outline', 'revision'],
) -> dict[str, str]:
    label = {'chapter': '正文生成', 'outline': '大纲生成', 'revision': '正文修订'}[operation]
    if isinstance(error, RequiredContextUnavailable):
        reason, detail = 'context_unavailable', '未能完整读取章节位置、前文衔接材料或人物与世界规则资料，请检查资料后重试'
        if str(error).startswith('character_cognition_'):
            detail = cognition_unavailable_message(error)
    elif isinstance(error, PipelineConflict):
        reason, detail = 'busy', '本书已有写作任务，请等待完成或先暂停整书任务'
    elif isinstance(error, StaleChapterWrite):
        reason, detail = 'source_changed', '章节已更新，请刷新核查后再操作'
    elif isinstance(error, TimeoutError):
        reason, detail = 'timeout', '请求超时'
    elif isinstance(error, ValueError) and str(error) == 'provider_incomplete_response':
        reason, detail = 'incomplete_response', '模型输出未完整结束'
    elif isinstance(error, ValueError) and str(error) == 'provider_empty_response':
        reason, detail = 'empty_response', '模型未返回有效正文'
    else:
        reason, detail = 'failed', '请求失败，请稍后重试或检查服务状态'
    return {
        'code': f'{operation}_{reason}',
        # Generation can have committed an earlier draft. An exception alone
        # cannot prove either that nothing changed or that a candidate is saved.
        'error': f'{label}：{detail}。已有片段请先保留，保存状态以保存确认或刷新核查结果为准。',
    }


def task_generation_error(error: Exception) -> str:
    """Required-context failures use the same actionable text in worker status."""
    if isinstance(error, RequiredContextUnavailable):
        return generation_error_payload(error, 'chapter')['error']
    return str(error) or type(error).__name__
