"""Claude 실행 토큰을 공개 API 단가로 환산한다."""

# Anthropic 공식 단가(USD / 100만 토큰), 2026-10-29 확인:
# https://platform.claude.com/docs/en/about-claude/pricing
# 캐시 쓰기는 5분 요금이다. CLI 집계에는 1시간 쓰기 구분이 없어 환산은 어림값이다.
# 일부 모델은 긴 문맥·지역·fast mode에 별도 요금이 적용될 수 있다.
PRICES = {
    "claude-fable-5-1": (10, 12.5, 0.25, 50),
    "claude-fable-5": (10, 12.5, 1, 50),
    "claude-opus-5-5": (4, 5, 0.20, 20),
    "claude-opus-5": (5, 6.25, 0.50, 25),
    "claude-opus-4-8": (5, 6.25, 0.50, 25),
    "claude-opus-4-7": (5, 6.25, 0.50, 25),
    "claude-opus-4-6": (5, 6.25, 0.50, 25),
    "claude-opus-4-5": (5, 6.25, 0.50, 25),
    "claude-opus-4-1": (15, 18.75, 1.50, 75),
    "claude-opus-4": (15, 18.75, 1.50, 75),
    "claude-sonnet-5-5": (2, 2.5, 0.10, 10),
    "claude-sonnet-5": (2, 2.5, 0.20, 10),
    "claude-sonnet-4-6": (3, 3.75, 0.30, 15),
    "claude-sonnet-4-5": (3, 3.75, 0.30, 15),
    "claude-sonnet-4": (3, 3.75, 0.30, 15),
    "claude-haiku-4-5": (1, 1.25, 0.10, 5),
    "claude-haiku-3-5": (0.8, 1, 0.08, 4),
}


def estimate_cost(*, model=None, input_tokens=None, output_tokens=None,
                  cache_write_tokens=None, cache_read_tokens=None, cost_usd=None):
    """실행 한 건의 환산액·어림 여부·환산 못 한 토큰 수를 돌려준다."""
    if cost_usd is not None:
        return {"cost_usd": cost_usd, "approximate": False, "unpriced_tokens": 0}

    tokens = (input_tokens or 0) + (output_tokens or 0)
    model = model or ""
    price = next((rates for name, rates in PRICES.items()
                  if model == name or model.startswith(name + "-")), None)
    if price is None or input_tokens is None or output_tokens is None:
        return {"cost_usd": None, "approximate": False, "unpriced_tokens": tokens}

    input_rate, write_rate, read_rate, output_rate = price
    separated = cache_write_tokens is not None and cache_read_tokens is not None
    if separated:
        ordinary = input_tokens - cache_write_tokens - cache_read_tokens
        if ordinary < 0:
            return {"cost_usd": None, "approximate": False, "unpriced_tokens": tokens}
        amount = ordinary * input_rate + cache_write_tokens * write_rate + cache_read_tokens * read_rate
    else:
        amount = input_tokens * input_rate
    amount += output_tokens * output_rate
    return {"cost_usd": amount / 1_000_000, "approximate": not separated, "unpriced_tokens": 0}
