"""LLM 工厂：可配置模型供应商（环境变量配置，任务书 0）。

- openai_compatible：任意 OpenAI 兼容端点（默认 DashScope/qwen）。
- mock：确定性 Mock（仅测试，明确标注，返回固定文本，供单元测试注入）。
真实端到端测试注入自定义 FakeChatModel，不经过本工厂。
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from mda.common.logging import get_logger
from mda.common.settings import Settings

log = get_logger(__name__)


class MockChatModel(BaseChatModel):
    """确定性 Mock LLM —— 仅测试使用，明确标注。"""

    response: str = "mock"

    @property
    def _llm_type(self) -> str:
        return "mock-chat"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])


def build_llm(settings: Settings | None = None) -> BaseChatModel:
    settings = settings or Settings()
    provider = settings.llm_provider.lower()
    if provider == "mock":
        return MockChatModel(response="mock")
    if provider == "openai_compatible":
        if not settings.effective_llm_key:
            log.warning(
                "llm_not_configured",
                provider=provider,
                error_code="MODEL_CONFIG_MISSING",
            )
            return MockChatModel(response="mock")
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        return ChatOpenAI(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=SecretStr(settings.effective_llm_key),
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
            max_retries=1,
        )
    raise ValueError(f"不支持的 LLM_PROVIDER: {provider!r}（支持 openai_compatible|mock）")
