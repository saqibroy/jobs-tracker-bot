#!/usr/bin/env python3
"""Retrieve Zoho Mail account IDs using saved .env OAuth credentials."""

from __future__ import annotations

import argparse
import asyncio
import getpass

from zoho_setup_common import (
    config_from_env,
    get_accounts,
    refresh_access_token,
    save_token_cache,
    update_env,
)


async def async_main() -> int:
    env = config_from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default=env["client_id"])
    parser.add_argument("--client-secret", default=env["client_secret"])
    parser.add_argument("--refresh-token", default=env["refresh_token"])
    parser.add_argument("--accounts-url", default=env["accounts_url"])
    parser.add_argument(
        "--save-first", action="store_true", help="Save the first account ID to .env"
    )
    args = parser.parse_args()

    client_secret = (
        args.client_secret or getpass.getpass("Zoho Client Secret: ").strip()
    )
    refresh_token = (
        args.refresh_token or getpass.getpass("Zoho Refresh Token: ").strip()
    )
    if not args.client_id:
        parser.error("--client-id is required or set ZOHO_CLIENT_ID in .env")
    if not client_secret or not refresh_token:
        parser.error("client secret and refresh token are required")

    token = await refresh_access_token(
        client_id=args.client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        accounts_url=args.accounts_url,
    )
    save_token_cache(token, token_file=env["token_file"])
    accounts = await get_accounts(
        access_token=token.access_token,
        mail_api_base=token.mail_api_base,
    )
    if not accounts:
        print("No Zoho Mail accounts returned.")
        return 1
    for account in accounts:
        suffix = f"  {account.email}" if account.email else ""
        print(f"{account.account_id}{suffix}")
    if args.save_first:
        update_env(
            {
                "ZOHO_ACCOUNT_ID": accounts[0].account_id,
                "ZOHO_MAIL_API_BASE": token.mail_api_base,
            }
        )
        print("Saved first account ID to .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
