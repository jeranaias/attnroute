"""
attnroute plugin system.

Plugins are discovered via:
1. Entry points: attnroute.plugins
2. Explicit registration in config
3. Built-in plugins (verifyfirst)
"""
import json
import sys
import threading
from pathlib import Path

from attnroute.plugins.base import AttnroutePlugin

# Global plugin registry
_plugins: dict[str, AttnroutePlugin] = {}
_discovered: bool = False
_lock = threading.Lock()


def discover_plugins() -> None:
    """Discover and register all available plugins."""
    global _discovered
    with _lock:
        if _discovered:
            return
        _discover_plugins_unlocked()
        _discovered = True


def _discover_plugins_unlocked() -> None:
    """Internal discovery logic (must be called with _lock held)."""

    # Method 1: Entry points (pip installed plugins)
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group="attnroute.plugins")

        for ep in eps:
            try:
                plugin_class = ep.load()
                _register_plugin_unlocked(plugin_class)
            except Exception as e:
                print(f"[attnroute] Plugin load failed for {ep.name}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[attnroute] Entry point discovery failed: {e}", file=sys.stderr)

    # Method 2: Built-in plugins
    try:
        from attnroute.plugins.verifyfirst import VerifyFirstPlugin
        _register_plugin_unlocked(VerifyFirstPlugin)
    except ImportError:
        pass

    try:
        from attnroute.plugins.loopbreaker import LoopBreakerPlugin
        _register_plugin_unlocked(LoopBreakerPlugin)
    except ImportError:
        pass

    try:
        from attnroute.plugins.burnrate import BurnRatePlugin
        _register_plugin_unlocked(BurnRatePlugin)
    except ImportError:
        pass


def _register_plugin_unlocked(plugin_class: type[AttnroutePlugin]) -> None:
    """Register a plugin class (must be called with _lock held)."""
    try:
        instance = plugin_class()
        if instance.is_enabled():
            _plugins[instance.name] = instance
    except Exception as e:
        print(f"[attnroute] Plugin registration failed for {plugin_class.__name__}: {e}", file=sys.stderr)


def register_plugin(plugin_class: type[AttnroutePlugin]) -> None:
    """Register a plugin class."""
    with _lock:
        _register_plugin_unlocked(plugin_class)


def get_plugins() -> list[AttnroutePlugin]:
    """Get all registered and enabled plugins."""
    global _discovered
    with _lock:
        if not _discovered:
            _discover_plugins_unlocked()
            _discovered = True
        return list(_plugins.values())


def get_plugin(name: str) -> AttnroutePlugin | None:
    """Get a specific plugin by name."""
    global _discovered
    with _lock:
        if not _discovered:
            _discover_plugins_unlocked()
            _discovered = True
        return _plugins.get(name)


def enable_plugin(name: str) -> bool:
    """Enable a plugin in config."""
    return _set_plugin_enabled(name, True)


def disable_plugin(name: str) -> bool:
    """Disable a plugin in config."""
    return _set_plugin_enabled(name, False)


def _set_plugin_enabled(name: str, enabled: bool) -> bool:
    """Set plugin enabled state in config (TOCTOU-safe)."""
    global _discovered, _plugins

    # Validate plugin name to prevent path traversal
    try:
        from attnroute.compat import validate_plugin_name, safe_atomic_write
        validate_plugin_name(name)
    except ImportError:
        # Fallback: basic validation
        if '/' in name or '\\' in name or '..' in name:
            print(f"[attnroute] Invalid plugin name: {name}", file=sys.stderr)
            return False
        safe_atomic_write = None
    except ValueError as e:
        print(f"[attnroute] {e}", file=sys.stderr)
        return False

    config_file = Path.home() / ".claude" / "plugins" / "config.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    # TOCTOU-safe: use try/except instead of exists()
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
        # Type validation: ensure we get a dict
        if not isinstance(config, dict):
            print("[attnroute] Config is not a dict, using defaults", file=sys.stderr)
            config = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        if not isinstance(e, FileNotFoundError):
            print(f"[attnroute] Config parse failed, using defaults: {e}", file=sys.stderr)
        config = {}

    if "enabled" not in config:
        config["enabled"] = {}

    config["enabled"][name] = enabled

    # Use atomic write if available
    content = json.dumps(config, indent=2)
    if safe_atomic_write:
        safe_atomic_write(config_file, content)
    else:
        config_file.write_text(content, encoding="utf-8")

    # Re-discover to apply changes
    with _lock:
        _discovered = False
        _plugins = {}
        _discover_plugins_unlocked()
        _discovered = True

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
