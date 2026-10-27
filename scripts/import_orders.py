"""Import orders from an operator-reviewed trusted source; default dry-run."""
import argparse
import asyncio
import json
from datetime import timezone
from decimal import Decimal
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from app.db.models import Order, User
from app.security.transactions import transaction


class ImportOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    product: str = Field(min_length=1, max_length=255)
    tracking_no: str | None = Field(default=None, max_length=64)
    refundable: bool = False
    refund_deadline: AwareDatetime | None = None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="Reviewed order-service export, JSON array")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rows = [ImportOrder.model_validate(item) for item in json.loads(Path(args.source).read_text(encoding="utf-8"))]
    if len(rows) > 10000 or len({r.order_id for r in rows}) != len(rows):
        raise SystemExit("Batch too large or duplicate order IDs")
    async with transaction() as s:
        for row in rows:
            if not await s.get(User, row.user_id):
                raise SystemExit("Destination user does not exist")
            existing = await s.get(Order, row.order_id)
            if existing and existing.user_id != row.user_id:
                raise SystemExit("Refusing to transfer order ownership")
            if args.apply:
                data = row.model_dump()
                if data["refund_deadline"]:
                    data["refund_deadline"] = data["refund_deadline"].astimezone(timezone.utc).replace(tzinfo=None)
                if existing:
                    for key, value in data.items():
                        setattr(existing, key, value)
                else:
                    s.add(Order(**data))
        print(f"Validated {len(rows)} orders; apply={args.apply}")


if __name__ == "__main__":
    asyncio.run(main())
