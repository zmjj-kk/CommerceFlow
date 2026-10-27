"""Reviewable explicit owner mapping. Default dry-run; run only during maintenance."""
import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import func, select, update
from app.db.models import Conversation, User
from app.security.transactions import transaction


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mapping", help="Reviewed JSON object: legacy_user_id -> authenticated user_id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or not mapping:
        raise SystemExit("Expected non-empty explicit owner mapping")
    async with transaction() as s:
        for old, new in mapping.items():
            if not isinstance(old, str) or not isinstance(new, str) or not old or not new:
                raise SystemExit("Invalid mapping")
            target = await s.get(User, new)
            if not target or not target.is_active:
                raise SystemExit("Destination user missing or disabled")
            if old != new and await s.get(User, old):
                raise SystemExit("Refusing to reassign an existing authenticated account")
        for old, new in mapping.items():
            count = await s.scalar(select(func.count()).select_from(Conversation).where(Conversation.user_id == old))
            print(json.dumps({"legacy_user_id": old, "user_id": new, "conversations": count,
                              "apply": args.apply}, ensure_ascii=False))
            if args.apply:
                await s.execute(update(Conversation).where(Conversation.user_id == old).values(user_id=new))
    print("Legacy pending interrupts remain invalid; start a new authenticated turn after migration.")


if __name__ == "__main__":
    asyncio.run(main())
