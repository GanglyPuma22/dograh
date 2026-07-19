import inspect
from pathlib import Path

from api.db.base_client import BaseDBClient
from api.db.embed_token_client import EmbedTokenClient
from api.logging_config import sanitize_sensitive_paths
from api.routes import public_embed


def test_sensitive_bearer_route_segments_are_redacted_from_log_messages():
    sentinel = "emb_session_sentinel_secret_value"
    message = (
        f'GET /api/v1/public/embed/turn-credentials/{sentinel}?transport=tcp 403 '
        f'ws://host/api/v1/ws/public/signaling/{sentinel}'
    )

    redacted = sanitize_sensitive_paths(message)

    assert sentinel not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_database_errors_hide_bound_session_token_parameters():
    client = BaseDBClient()

    assert client.engine.sync_engine.hide_parameters is True


def test_embed_session_and_turn_sources_never_log_or_derive_from_the_bearer_token():
    create_source = inspect.getsource(EmbedTokenClient.create_embed_session)
    route_source = inspect.getsource(public_embed.get_public_turn_credentials)

    assert 'logger.info(f"Created embed session {session_token}")' not in create_source
    assert "session_token[:" not in route_source


def test_remote_nginx_disables_request_uri_access_logs_on_http_and_https():
    template = (
        Path(__file__).resolve().parents[2]
        / "deploy/templates/nginx.remote.conf.template"
    ).read_text()

    assert template.count("access_log off;") == 2
