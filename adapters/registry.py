from adapters.agents.claude_agent import ClaudeAgent
from adapters.services.chatgpt_service import ChatGPTService
from adapters.services.claude_service import ClaudeService
from adapters.services.gemini_service import GeminiService

_ADAPTERS = {
    "claude_service": ClaudeService,
    "gemini_service": GeminiService,
    "chatgpt_service": ChatGPTService,
    "claude_agent": ClaudeAgent
}


def initialize_adapter(adapter: str) -> ClaudeService | GeminiService | ChatGPTService | ClaudeAgent:
    """Instantiate an adapter by name."""
    try:
        return _ADAPTERS[adapter]()
    except KeyError:
        raise ValueError(f"unknown adapter '{adapter}' (expected one of {sorted(_ADAPTERS)})") from None
