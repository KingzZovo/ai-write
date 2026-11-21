"""Bounded public diagnostics for selection rewrites."""


class RewriteContextLimitError(ValueError):
    pass


def rewrite_error_payload(error: Exception) -> dict[str, str]:
    if isinstance(error, RewriteContextLimitError):
        code, message = 'rewrite_context_too_large', '改写材料超出本次上下文预算，请缩小选段或参考范围后重试。原文未被修改。'
    elif isinstance(error, ValueError) and str(error) == 'provider_incomplete_response':
        code, message = 'rewrite_incomplete_response', '模型未完整返回改写结果，本次候选不可采用，原文未被修改。'
    elif isinstance(error, ValueError) and str(error) == 'provider_empty_response':
        code, message = 'rewrite_empty_response', '模型未返回有效正文，请重试。原文未被修改。'
    elif isinstance(error, TimeoutError):
        code, message = 'rewrite_timeout', '改写请求超时，本次候选不可采用，原文未被修改。'
    else:
        code, message = 'rewrite_failed', '改写请求失败，本次候选不可采用。请稍后重试或检查模型配置，原文未被修改。'
    return {'error': message, 'code': code}
