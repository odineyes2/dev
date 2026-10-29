"""Claude 공개 API 단가 환산의 비용 우선순위와 캐시·구기록을 확인한다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
from api_pricing import estimate_cost  # noqa: E402


assert estimate_cost(model='claude-sonnet-4-6', input_tokens=1_000_000,
                     output_tokens=1_000_000, cost_usd=0.42) == {
    'cost_usd': 0.42, 'approximate': False, 'unpriced_tokens': 0}

cached = estimate_cost(model='claude-sonnet-4-6-20261001', input_tokens=1_000_000,
                       output_tokens=1_000_000, cache_write_tokens=200_000,
                       cache_read_tokens=300_000)
assert cached == {'cost_usd': 17.34, 'approximate': False, 'unpriced_tokens': 0}, cached

legacy = estimate_cost(model='claude-opus-5', input_tokens=1_000_000,
                       output_tokens=100_000)
assert legacy == {'cost_usd': 7.5, 'approximate': True, 'unpriced_tokens': 0}, legacy

unknown = estimate_cost(model='gpt-6-sol', input_tokens=300, output_tokens=20)
assert unknown == {'cost_usd': None, 'approximate': False, 'unpriced_tokens': 320}, unknown

print('OK API pricing')
