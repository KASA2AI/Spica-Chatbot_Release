#!/usr/bin/env python
"""Launch the independent, loopback-only Spica Config Studio sidecar."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
from typing import Any, Callable, Sequence
from urllib.parse import quote
import webbrowser


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import uvicorn  # noqa: E402

from spica.adapters.config_studio.platform import (  # noqa: E402
    current_platform_capabilities,
    linux_self_check_base_environment,
)
from spica.adapters.config_studio.self_check_process import (  # noqa: E402
    SubprocessSelfCheckRunner,
)
from spica.config.secrets import LoadedSecrets, load_secrets  # noqa: E402
from spica.ports.config_studio_platform import PlatformCapabilities  # noqa: E402
from spica.config_studio.api import create_config_studio_app  # noqa: E402
from spica.config_studio.assets import load_static_ui_assets  # noqa: E402
from spica.config_studio.security import SecurityContext  # noqa: E402
from spica.config_studio.server import LoopbackServer  # noqa: E402
from spica.config_studio.self_check_service import (  # noqa: E402
    SelfCheckEnvironmentInputs,
    create_production_self_check_service,
)
from ui.config_studio.composition import (  # noqa: E402
    create_production_config_studio_services,
)


_AUTO_BACKGROUND_HEALTH = object()
_BROWSER_FALLBACK_DELAY_SECONDS = 5.0


def _schedule_fallback(
    delay_seconds: float,
    callback: Callable[[], None],
) -> threading.Timer:
    timer = threading.Timer(delay_seconds, callback)
    timer.daemon = True
    timer.start()
    return timer


def _write_terminal_fallback_if_pending(
    *,
    security_context: SecurityContext,
    bootstrap_token: str,
    terminal_write: Callable[[str], object],
) -> None:
    """Intentionally disclose one pending grant to the launching terminal."""

    if not security_context.bootstrap_is_pending():
        return
    terminal_write("Config Studio is waiting for a local browser session.")
    terminal_write(f"Open: {security_context.origin}/")
    terminal_write(f"Paste one-time bootstrap grant: {bootstrap_token}")


def _production_port(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Spica Local Config Studio (loopback sidecar)",
    )
    parser.add_argument(
        "--port",
        type=_production_port,
        default=8765,
        help="loopback TCP port (1024-65535; default: 8765)",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="do not open the default browser automatically",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path | None = None,
    server_bind: Callable[..., Any] = LoopbackServer.bind,
    server_factory: Callable[[Any], Any] = uvicorn.Server,
    browser_open: Callable[..., Any] = webbrowser.open,
    token_factory: Callable[[], str] | None = None,
    terminal_write: Callable[[str], object] = print,
    fallback_scheduler: Callable[
        [float, Callable[[], None]], Any
    ] = _schedule_fallback,
    background_health_code: object = _AUTO_BACKGROUND_HEALTH,
    platform_capabilities: PlatformCapabilities | None = None,
) -> int:
    loaded = load_secrets(
        with_environment_snapshot=True,
        prime_process=False,
    )
    args = _argument_parser().parse_args(argv)
    if not isinstance(loaded, LoadedSecrets):
        raise RuntimeError("CONFIG_STUDIO_ENVIRONMENT_SNAPSHOT_UNAVAILABLE")
    selected_platform = (
        current_platform_capabilities()
        if platform_capabilities is None
        else platform_capabilities
    )
    if not isinstance(selected_platform, PlatformCapabilities):
        raise TypeError("platform_capabilities must be PlatformCapabilities")

    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    if background_health_code is _AUTO_BACKGROUND_HEALTH:
        resolved_background_health = (
            load_static_ui_assets().background.health_code
        )
    elif background_health_code in (None, "BACKGROUND_ASSET_INVALID"):
        resolved_background_health = background_health_code
    else:
        raise ValueError("unsupported background health code")

    def latest_environment() -> LoadedSecrets:
        return loaded.refresh()

    def self_check_environment_inputs() -> SelfCheckEnvironmentInputs:
        latest = latest_environment()
        return SelfCheckEnvironmentInputs(
            environment_snapshot=latest.environment_snapshot,
            secrets=latest.secrets,
            legacy_secret_canaries=latest.legacy_secret_canaries,
            secret_material_sanitizer=latest.sanitize_secret_material,
        )

    self_check_runner = None
    self_check_base_environment = None
    if selected_platform.self_check_containment:
        try:
            self_check_runner = SubprocessSelfCheckRunner(
                repo_root=root,
                platform_capabilities=selected_platform,
            )
            self_check_base_environment = linux_self_check_base_environment(
                selected_platform
            )
        except Exception:  # noqa: BLE001 -- optional capability fails closed
            self_check_runner = None
            self_check_base_environment = None
    self_check_service = create_production_self_check_service(
        repo_root=root,
        environment_inputs=self_check_environment_inputs,
        platform_capabilities=selected_platform,
        runner=self_check_runner,
        base_child_environment=self_check_base_environment,
    )
    services = create_production_config_studio_services(
        repo_root=root,
        loaded_environment=loaded,
        environment_owner=latest_environment,
        platform_capabilities=selected_platform,
        background_health_code=resolved_background_health,
        self_check_service=self_check_service,
    )
    fallback_handle: Any = None
    try:
        with server_bind(host="127.0.0.1", port=args.port) as server:
            issue_options: dict[str, Any] = {
                "host": server.host,
                "port": server.port,
            }
            if token_factory is not None:
                issue_options["token_factory"] = token_factory
            grant = SecurityContext.issue(**issue_options)
            app = create_config_studio_app(services, grant.security_context)
            if args.no_open_browser:
                _write_terminal_fallback_if_pending(
                    security_context=grant.security_context,
                    bootstrap_token=grant.bootstrap_token,
                    terminal_write=terminal_write,
                )
            else:
                bootstrap = quote(grant.bootstrap_token, safe="")
                launch_url = f"{grant.security_context.origin}/#bootstrap={bootstrap}"
                try:
                    browser_started = bool(browser_open(launch_url, new=2))
                except Exception:  # noqa: BLE001 -- browser launch is optional
                    browser_started = False
                if browser_started:
                    fallback_handle = fallback_scheduler(
                        _BROWSER_FALLBACK_DELAY_SECONDS,
                        lambda: _write_terminal_fallback_if_pending(
                            security_context=grant.security_context,
                            bootstrap_token=grant.bootstrap_token,
                            terminal_write=terminal_write,
                        ),
                    )
                else:
                    _write_terminal_fallback_if_pending(
                        security_context=grant.security_context,
                        bootstrap_token=grant.bootstrap_token,
                        terminal_write=terminal_write,
                    )
            config = server.uvicorn_config(app)
            server_factory(config).run(sockets=[server.socket])
    finally:
        cancel_fallback = getattr(fallback_handle, "cancel", None)
        if callable(cancel_fallback):
            cancel_fallback()
        services.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
