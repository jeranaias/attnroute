"""
attnroute plugin system.

Plugins are discovered via:
1. Entry points: attnroute.plugins
2. Explicit registration in config
3. Built-in plugins (verifyfirst)
"""
import json
from pathlib import Path

from attnroute.plugins.base import AttnroutePlugin

# Global plugin registry
_plugins: dict[str, AttnroutePlugin] = {}
_discovered: bool = False


def discover_plugins() -> None:
    """Discover and register all available plugins."""
    global _discovered
    if _discovered:
        return

    # Method 1: Entry points (pip installed plugins)
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group="attnroute.plugins")

        for ep in eps:
            try:
                plugin_class = ep.load()
                register_plugin(plugin_class)
            except Exception:
                pass
    except Exception:
        pass

    # Method 2: Built-in plugins
    try:
        from attnroute.plugins.verifyfirst import VerifyFirstPlugin
        register_plugin(VerifyFirstPlugin)
    except ImportError:
        pass

    try:
        from attnroute.plugins.loopbreaker import LoopBreakerPlugin
        register_plugin(LoopBreakerPlugin)
    except ImportError:
        pass

    try:
        from attnroute.plugins.burnrate import BurnRatePlugin
        register_plugin(BurnRatePlugin)
    except ImportError:
        pass

    _discovered = True


def register_plugin(plugin_class: type[AttnroutePlugin]) -> None:
    """Register a plugin class."""
    try:
        instance = plugin_class()
        if instance.is_enabled():
            _plugins[instance.name] = instance
    except Exception:
        pass


def get_plugins() -> list[AttnroutePlugin]:
    """Get all registered and enabled plugins."""
    discover_plugins()
    return list(_plugins.values())


def get_plugin(name: str) -> AttnroutePlugin | None:
    """Get a specific plugin by name."""
    discover_plugins()
    return _plugins.get(name)


def enable_plugin(name: str) -> bool:
    """Enable a plugin in config."""
    return _set_plugin_enabled(name, True)


def disable_plugin(name: str) -> bool:
    """Disable a plugin in config."""
    return _set_plugin_enabled(name, False)


def _set_plugin_enabled(name: str, enabled: bool) -> bool:
    """Set plugin enabled state in config."""
    config_file = Path.home() / ".claude" / "plugins" / "config.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    config = {}
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    if "enabled" not in config:
        config["enabled"] = {}

    config["enabled"][name] = enabled
    config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Re-discover to apply changes
    global _discovered, _plugins
    _discovered = False
    _plugins = {}
    discover_plugins()

    return True


# Export public API
__all__ = [
    "AttnroutePlugin",
    "discover_plugins",
    "register_plugin",
    "get_plugins",
    "get_plugin",
    "enable_plugin",
    "disable_plugin",
]
