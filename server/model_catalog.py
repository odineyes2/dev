"""모델 카탈로그 — vendor(anthropic/openai) → provider(claude/codex) → 고를 수 있는 모델 목록.
REST `GET /api/model-catalog`와 MCP `list_models`가 같은 데이터를 준다. 목록이 낡으면 이 상수를 고친다.

출처(DEV-89 계획서 v1, 2026-10-06 확인):
- Anthropic: https://github.com/anthropics/skills/blob/main/skills/claude-api/shared/models.md
  제외: claude-mythos-5-1/claude-mythos-5(Project Glasswing 전용), claude-opus-4-1(2026-08-05 종료)
- OpenAI: https://developers.openai.com/codex/models , https://flaviocopes.com/gpt-6-1-sol/
  제외: GPT-5.5(2026-10-14 Codex 종료 예정). ID는 검색 결과로 확인 — 실제 계정에서 받는지는 사람이 확인한다.
"""

VENDORS = [
    {"vendor": "anthropic", "name": "Anthropic", "provider": "claude", "models": [
        {"id": "claude-fable-5-1", "name": "Fable 5.1", "note": "최상위"},
        {"id": "claude-fable-5", "name": "Fable 5", "note": ""},
        {"id": "claude-opus-5-5", "name": "Opus 5.5", "note": "기본 Opus"},
        {"id": "claude-opus-5", "name": "Opus 5", "note": ""},
        {"id": "claude-opus-4-8", "name": "Opus 4.8", "note": ""},
        {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5", "note": ""},
        {"id": "claude-sonnet-5", "name": "Sonnet 5", "note": ""},
        {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6", "note": ""},
        {"id": "claude-haiku-4-5", "name": "Haiku 4.5", "note": ""},
    ]},
    {"vendor": "openai", "name": "OpenAI", "provider": "codex", "models": [
        {"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol", "note": "최신"},
        {"id": "gpt-6-sol", "name": "GPT-6 Sol", "note": ""},
        {"id": "gpt-6-luna", "name": "GPT-6 Luna", "note": "빠르고 저렴"},
    ]},
]


def catalog() -> dict:
    """화면·API·MCP용 카탈로그 — vendors 목록과 vendor→provider 매핑."""
    return {"vendors": VENDORS, "providers": {v["vendor"]: v["provider"] for v in VENDORS}}
