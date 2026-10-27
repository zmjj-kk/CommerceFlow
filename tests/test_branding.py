from pathlib import Path
import tomllib

from app.api.auth import COOKIE
from app.main import app


def test_application_and_refresh_cookie_use_commerceflow():
    assert app.title == "CommerceFlow"
    assert COOKIE == "commerceflow_refresh"


def test_project_metadata_and_frontend_namespace():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "commerceflow"
    for name in ("login.html", "index.html"):
        text = (root / "app/static" / name).read_text(encoding="utf-8")
        assert "CommerceFlow" in text
    auth = (root / "app/static/auth.js").read_text(encoding="utf-8")
    assert "window.commerceflowAuth" in auth
    assert "commerceflow_conversation_id" in auth
