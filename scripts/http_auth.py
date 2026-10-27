"""Bearer headers for opt-in end-to-end scripts. Never embeds or logs credentials."""
import os


def bearer_headers() -> dict[str, str]:
    token = os.environ.get("COMMERCEFLOW_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("Set COMMERCEFLOW_ACCESS_TOKEN to a current access token from /api/auth/login")
    return {"Authorization": f"Bearer {token}"}
