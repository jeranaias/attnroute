"""Base class for attnroute plugins."""
import json
from abc import ABC
from pathlib import Path


class AttnroutePlugin(ABC):
    """
    Base class for attnroute plugins.

    Lifecycle hooks:
    - on_session_start: New session begins
    - on_prompt_pre: Before context routing (can modify prompt)
    - on_prompt_post: After routing (can add context)
    - on_stop: After Claude finishes (access to tool calls)

    State is persisted per-plugin in ~/.claude/plugins/{plugin_name}_state.json
    """

    # Plugin metadata (override in subclass)
    name: str = "base"
    version: str = "0.1.0"
    description: str = "Base plugin"

    # State management
    _state_dir = Path.home() / ".claude" / "plugins"

    def __init__(self):
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._state_dir / f"{self.name}_state.json"

    def load_state(self) -> dict:
        """Load plugin-specific state from disk."""
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def save_state(self, state: dict) -> None:
        """Save plugin-specific state to disk."""
        self._state_file.write_text(
            json.dumps(state, indent=2, default=str),
            encoding="utf-8"
        )

    # Lifecycle hooks (override as needed)

    def on_session_start(self, session_state: dict) -> str | None:
        """
        Called on SessionStart hook.

        Args:
            session_state: Global session state dict

        Returns:
            Optional string to append to session dashboard output
        """
        return None

    def on_prompt_pre(self, prompt: str, session_state: dict) -> tuple[str, bool]:
        """
        Called before context routing.

        Args:
            prompt: User's prompt text
            session_state: Global session state dict

        Returns:
            Tuple of (modified_prompt, should_continue)
            If should_continue is False, context routing is skipped
        """
        return prompt, True

    def on_prompt_post(
        self,
        prompt: str,
        context_output: str,
        session_state: dict
    ) -> str:
        """
        Called after context routing.

        Args:
            prompt: User's prompt text
            context_output: Context that will be injected
            session_state: Global session state dict

        Returns:
            Additional context to append (or empty string)
        """
        return ""

    def on_stop(self, tool_calls: list[dict], session_state: dict) -> str | None:
        """
        Called on Stop hook after Claude finishes.

        Args:
            tool_calls: List of tool calls Claude made
            session_state: Global session state dict

        Returns:
            Optional warning/info message to log
        """
        return None

    def is_enabled(self) -> bool:
        """Check if this plugin is enabled in config."""
        config_file = Path.home() / ".claude" / "plugins" / "config.json"
        if config_file.exists():
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
                return config.get("enabled", {}).get(self.name, True)
            except Exception:
                pass
        return True  # Enabled by default if config missing
