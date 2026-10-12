from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import __version__
from app import api as api_package
from app.api import discover_router_modules, include_routers
from app.config import Settings
from app.main import create_app


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_lifespan_creates_data_dirs(client: TestClient, settings: Settings) -> None:
    for path in (settings.data_dir, settings.browser_profiles_dir, settings.sessions_dir, settings.logs_dir):
        assert path.is_dir()


def test_spa_fallback(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    test_client = TestClient(create_app(frontend_dist=dist))
    assert test_client.get("/").text == "<html>spa</html>"
    assert test_client.get("/trips/12").text == "<html>spa</html>"
    assert test_client.get("/assets/app.js").text == "console.log(1)"
    assert test_client.get("/../secret.txt").text == "<html>spa</html>"
    assert test_client.get("/api/does-not-exist").status_code == 404
    assert test_client.get("/api/health").json()["status"] == "ok"


def test_spa_does_not_turn_unknown_api_paths_into_405(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    test_client = TestClient(create_app(frontend_dist=dist))
    for method, path in [("POST", "/api/nope"), ("DELETE", "/api/no-such-resource/1"), ("PUT", "/api")]:
        response = test_client.request(method, path)
        assert response.status_code == 404, (method, path)
        assert response.json() == {"detail": "Not Found"}
    assert test_client.post("/api/health").status_code == 405  # known path, wrong method
    assert test_client.get("/apiary").text == "<html>spa</html>"  # not the /api prefix


def test_include_routers_discovers_new_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/zz-probe')\n"
        "def probe() -> dict:\n"
        "    return {'ok': True}\n"
    )
    (tmp_path / "zz_probe.py").write_text(probe_source, encoding="utf-8")
    (tmp_path / "_zz_helper.py").write_text("router = 'not mounted'\n", encoding="utf-8")
    (tmp_path / "zz_plain.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(api_package, "__path__", [*api_package.__path__, str(tmp_path)])
    for name in ("zz_probe", "_zz_helper", "zz_plain"):
        monkeypatch.delitem(sys.modules, f"app.api.{name}", raising=False)

    names = discover_router_modules()
    assert "zz_probe" in names and "zz_plain" in names and "_zz_helper" not in names
    app = FastAPI()
    included = include_routers(app)
    assert "zz_probe" in included and "zz_plain" not in included
    assert TestClient(app).get("/api/zz-probe").json() == {"ok": True}
    for name in ("zz_probe", "zz_plain"):
        sys.modules.pop(f"app.api.{name}", None)


def test_include_routers_rejects_non_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "zz_bad.py").write_text("router = object()\n", encoding="utf-8")
    monkeypatch.setattr(api_package, "__path__", [*api_package.__path__, str(tmp_path)])
    monkeypatch.delitem(sys.modules, "app.api.zz_bad", raising=False)
    with pytest.raises(TypeError, match="APIRouter"):
        include_routers(FastAPI())
    sys.modules.pop("app.api.zz_bad", None)
