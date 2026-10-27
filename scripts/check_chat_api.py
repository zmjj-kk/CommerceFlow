"""Validate configured chat provider without exposing credentials."""
from app.config import settings
import httpx

try:
    response = httpx.get(
        settings.chat_base_url.rstrip("/") + "/models",
        headers={"Authorization": "Bearer " + settings.chat_api_key},
        timeout=20,
    )
    print("chat_provider_http_status:", response.status_code)
    if response.is_success:
        available = {item.get("id") for item in response.json().get("data", [])}
        print("configured_model_available:", settings.chat_model in available)
    else:
        print("chat_provider_request_failed")
except Exception as exc:
    print("chat_provider_connection_failed:", type(exc).__name__)
