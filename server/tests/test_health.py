from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from arc import __version__
from arc.config import Settings
from arc.main import create_app


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["env"] == "test"


async def test_openapi_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/health" in response.json()["paths"]


async def test_the_docs_are_off_in_production(settings: Settings) -> None:
    """A complete map of the API, published to anyone who asks.

    Useful in development, and read by nothing in production — Arc's own
    client is built against a hand-written type module, not the schema.
    """
    prod = create_app(settings.model_copy(update={"env": "prod"}))

    async with AsyncClient(transport=ASGITransport(app=prod), base_url="http://test") as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404, path

        # Not a blanket 404: the app itself is up.
        assert (await client.get("/api/health")).status_code == 200


async def test_health_reports_whether_tmdb_is_configured(settings: Settings) -> None:
    """The client reads this to decide whether to show TMDB's attribution."""
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings)), base_url="http://test"
    ) as client:
        assert (await client.get("/api/health")).json()["tmdb_enabled"] is False

    with_key = settings.model_copy(update={"tmdb_api_key": "a-real-tmdb-key"})
    async with AsyncClient(
        transport=ASGITransport(app=create_app(with_key)), base_url="http://test"
    ) as client:
        assert (await client.get("/api/health")).json()["tmdb_enabled"] is True
