"""Token authentication and the single error envelope."""

import pytest

from app.config import get_settings
from app.deps import require_service_token
from app.errors import UnauthorizedError
from tests.conftest import ADMIN_HEADERS

PROTECTED = [
    ("get", "/admin/plans"),
    ("post", "/admin/plans"),
    ("get", "/admin/plans/pro"),
    ("patch", "/admin/plans/pro"),
    ("get", "/admin/keys"),
    ("post", "/admin/keys"),
    ("get", "/admin/keys/k_1"),
    ("patch", "/admin/keys/k_1"),
    ("post", "/admin/keys/k_1/revoke"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}])
async def test_admin_routes_require_the_admin_token(client, method, path, headers):
    response = await getattr(client, method)(path, headers=headers)

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "unauthorized",
            "message": "A valid X-Admin-Token header is required.",
        }
    }


async def test_health_is_open(client):
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_service_token_is_a_no_op_while_unset():
    settings = get_settings()
    assert settings.qg_service_token is None

    assert require_service_token(settings, None) is None
    assert require_service_token(settings, "anything") is None


async def test_service_token_is_enforced_once_configured():
    settings = get_settings().model_copy(update={"qg_service_token": "service-token"})

    assert require_service_token(settings, "service-token") is None
    with pytest.raises(UnauthorizedError):
        require_service_token(settings, None)
    with pytest.raises(UnauthorizedError):
        require_service_token(settings, "wrong")


async def test_tokens_never_appear_in_error_bodies(client):
    response = await client.get("/admin/plans", headers={"X-Admin-Token": "leak-me"})

    assert "leak-me" not in response.text
    assert get_settings().qg_admin_token not in response.text


async def test_unknown_route_uses_the_error_envelope(client):
    response = await client.get("/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_malformed_json_uses_the_error_envelope(client):
    response = await client.post(
        "/admin/plans",
        content=b"{not json",
        headers={**ADMIN_HEADERS, "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    # A body offset must not surface as a field name.
    assert not response.json()["error"]["message"].startswith("1:")


async def test_responses_echo_a_request_id(client):
    generated = await client.get("/health")
    echoed = await client.get("/health", headers={"X-Request-ID": "abc123"})

    assert generated.headers["X-Request-ID"]
    assert echoed.headers["X-Request-ID"] == "abc123"
