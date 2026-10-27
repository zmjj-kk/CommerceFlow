"""Offline account bootstrap; never accepts passwords as command-line arguments."""
import argparse
import asyncio
from getpass import getpass

from app.services.auth_service import create_user


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    parser.add_argument("--admin", action="store_true")
    args = parser.parse_args()
    password = getpass("Password (12-128 chars, letters + numbers): ")
    if password != getpass("Confirm password: "):
        raise SystemExit("Passwords differ")
    user = await create_user(args.username, password, role="admin" if args.admin else "user")
    print(f"Created {user.username} user_id={user.user_id} role={user.role}")


if __name__ == "__main__":
    asyncio.run(main())
