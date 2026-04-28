from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from pliny.api.deps import require_api_key


async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_protected_route_no_auth() -> None:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected() -> dict[str, str]:
        return {"ok": "1"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/protected")
        assert r.status_code == 401

        r = await ac.get("/protected", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

        r = await ac.get("/protected", headers={"Authorization": "Bearer test-key"})
        assert r.status_code == 200
