from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_favicon_returns_success() -> None:
    client = TestClient(app)
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
