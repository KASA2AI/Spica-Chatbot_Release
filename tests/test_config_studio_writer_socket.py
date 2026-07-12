from __future__ import annotations

from http.cookies import SimpleCookie
import http.client
import json
import os
import threading
import time

import uvicorn

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config.secrets import load_secrets
from spica.config_studio.api import create_config_studio_app
from spica.config_studio.security import SecurityContext
from spica.config_studio.server import LoopbackServer
from spica.adapters.config_studio.composition import (
    create_production_config_studio_services,
)


def test_real_loopback_socket_commits_app_and_keeps_secret_canary_opaque(
    tmp_path,
):
    repo_root = tmp_path / "synthetic-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    repo_env = repo_root / "xiaosan.env"
    parent_env = tmp_path / "synthetic-parent" / "xiaosan.env"
    app_path.parent.mkdir(parents=True)
    repo_env.parent.mkdir(parents=True, exist_ok=True)
    parent_env.parent.mkdir(parents=True)
    app_path.write_text("max_tool_rounds: 2\n", encoding="utf-8")
    secret_canary = "socket-secret-canary-never-return"
    repo_env.write_text(
        f"OPENAI_API_KEY='{secret_canary}'\n",
        encoding="utf-8",
    )
    repo_env.chmod(0o600)
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    platform = platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=os.getuid(),
        temp_directory=tmp_path / "platform-tmp",
    )
    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=loaded.refresh,
        platform_capabilities=platform,
        background_health_code="BACKGROUND_ASSET_INVALID",
        self_check_service=None,
    )
    assert services.capability_enabled("app_config_write") is True

    bodies: list[bytes] = []
    with LoopbackServer.bind(port=0, allow_test_port_zero=True) as bound:
        security = SecurityContext(
            host=bound.host,
            port=bound.port,
            bootstrap_token="socket-bootstrap-token",
            clock=lambda: 100.0,
            token_factory=iter(["socket-session", "socket-csrf"]).__next__,
        )
        app = create_config_studio_app(services, security)
        server = uvicorn.Server(bound.uvicorn_config(app))
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [bound.socket]},
            daemon=True,
        )
        thread.start()
        try:
            deadline = time.monotonic() + 3.0
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            connection = http.client.HTTPConnection(
                bound.host,
                bound.port,
                timeout=2,
            )
            try:
                origin = f"http://{bound.host}:{bound.port}"
                connection.request(
                    "POST",
                    "/api/v1/session/bootstrap",
                    headers={
                        "Origin": origin,
                        "X-Spica-Bootstrap": "socket-bootstrap-token",
                    },
                )
                response = connection.getresponse()
                bootstrap_body = response.read()
                bodies.append(bootstrap_body)
                assert response.status == 200
                bootstrap = json.loads(bootstrap_body)
                cookies = SimpleCookie()
                cookies.load(response.getheader("Set-Cookie"))
                session_cookie = cookies["spica_config_studio_session"]
                authenticated = {
                    "Cookie": (
                        "spica_config_studio_session=" + session_cookie.value
                    )
                }
                write_headers = {
                    **authenticated,
                    "Content-Type": "application/json",
                    "Origin": origin,
                    "X-Spica-CSRF": bootstrap["csrf_token"],
                }

                for route in (
                    "/api/v1/meta",
                    "/api/v1/catalog",
                    "/api/v1/sensitive/status",
                ):
                    connection.request("GET", route, headers=authenticated)
                    read_response = connection.getresponse()
                    body = read_response.read()
                    bodies.append(body)
                    assert read_response.status == 200

                preview_request = json.dumps(
                    {
                        "operations": [
                            {
                                "kind": "set",
                                "path": [
                                    {
                                        "kind": "field",
                                        "name": "max_tool_rounds",
                                    }
                                ],
                                "value": 3,
                            }
                        ]
                    }
                ).encode("utf-8")
                connection.request(
                    "POST",
                    "/api/v1/app/previews",
                    body=preview_request,
                    headers=write_headers,
                )
                preview_response = connection.getresponse()
                preview_body = preview_response.read()
                bodies.append(preview_body)
                assert preview_response.status == 200
                preview_id = json.loads(preview_body)["preview_id"]

                connection.request(
                    "POST",
                    "/api/v1/app/commits",
                    body=json.dumps({"preview_id": preview_id}).encode("utf-8"),
                    headers=write_headers,
                )
                commit_response = connection.getresponse()
                commit_body = commit_response.read()
                bodies.append(commit_body)
                assert commit_response.status == 200
                assert json.loads(commit_body)["status"] == "saved"
            finally:
                connection.close()
        finally:
            server.should_exit = True
            thread.join(timeout=3)

    assert not thread.is_alive()
    assert app_path.read_text(encoding="utf-8") == "max_tool_rounds: 3\n"
    assert secret_canary.encode("utf-8") not in b"\n".join(bodies)
    assert repo_env.read_text(encoding="utf-8") == (
        f"OPENAI_API_KEY='{secret_canary}'\n"
    )
