"""
Core Brain Orchestrator for Star Assistant.
Connects the query understanding, tool execution, and voice synthesis.
"""

from typing import Dict, Any, Optional

from .llm.provider import get_llm_provider
from .voice.tts import synthesize_speech
from .agent_bridge import get_agent_bridge


class AssistantBrain:
    """Orchestrates query processing, tool execution, memory, and voice output."""

    def __init__(self):
        self.provider = get_llm_provider()
        self.agent = get_agent_bridge()

    def process(self, query: str) -> Dict[str, Any]:
        """Process user input query and return full result package."""
        if not query or not query.strip():
            return {
                "response": "বলো বন্ধু, আমি শুনছি!",
                "audio_path": None,
                "actions": [],
                "source": "empty_prompt"
            }

        # 1. Fetch semantic memory context
        memory_context = self.agent.get_memory_context(query)

        # 2. Process with Provider (Local LLM, Gemini, or Offline Engine)
        result = self.provider.process_query(query, memory_context=memory_context)
        response_text = result.get("response", "")
        actions = result.get("actions", [])

        # 3. Persist episodic memory for genuine CONVERSATION only.
        # FIX: command/action turns used to be stored as episodes too — the
        # memory then filled with junk like 'volume barao -> adjust_volume',
        # and that junk was later injected into prompts / dumped as answers.
        clean_q = query.strip().lower()
        is_command = bool(actions) or result.get("source", "").startswith(
            ("command_router", "offline_intent", "screen_vision", "memory_store")
        )
        if response_text and len(clean_q) > 3 and not is_command:
            if not any(clean_q == g for g in ["hi", "hello", "hey", "হাই", "হ্যালো", "শোনো"]):
                self.agent.remember(f"User said: '{query}' -> Assistant answered: '{response_text}'", kind="episode")

        # 4. Synthesize friendly spoken audio
        audio_path = None
        if response_text:
            try:
                audio_path = synthesize_speech(response_text)
            except Exception as e:
                print(f"[Brain] Speech synthesis warning: {e}")

        return {
            "response": response_text,
            "audio_path": audio_path,
            "actions": actions,
            "source": result.get("source", "brain"),
            "memory_context": memory_context
        }
