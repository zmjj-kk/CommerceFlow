from app.tools.builtin.tickets import create_ticket
from tests.security.conftest import encoded_password, secure_db


def test_all_injected_security_fields_hidden():
    props = create_ticket.tool_call_schema.model_json_schema()["properties"]
    assert set(props) == {"description", "ticket_type"}


async def test_direct_tool_requires_bound_approval(secure_db):
    from pydantic import ValidationError
    import pytest
    with pytest.raises(ValidationError):
        await create_ticket.ainvoke({"description": "商品损坏", "ticket_type": "售后",
                                    "conversation_id": 1, "user_id": "alice"})
