#!/usr/bin/env python3
"""Exchange a Zoho authorization code for OAuth tokens and update .env."""

from __future__ import annotations

import argparse
import asyncio
import getpass

from zoho_setup_common import (
    TOKEN_PATH_DEFAULT,
    ZOHO_DATA_CENTERS,
    ZohoSetupConfig,
    config_from_env,
    exchange_authorization_code,
    save_token_cache,
    update_env,
)


async def async_main() -> int:
    env = config_from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", help="Authorization code from Zoho redirect")
    parser.add_argument("--client-id", default=env["client_id"])
    parser.add_argument("--client-secret", default=env["client_secret"])
    parser.add_argument("--accounts-url", default=env["accounts_url"])
    parser.add_argument(
        "--dc", choices=sorted(ZOHO_DATA_CENTERS), help="Zoho data center shortcut"
    )
    parser.add_argument("--redirect-uri", default=env["redirect_uri"])
    parser.add_argument(
        "--self-client",
        action="store_true",
        help="Exchange a Self Client grant code without redirect_uri.",
    )
    parser.add_argument("--token-file", default=env["token_file"] or TOKEN_PATH_DEFAULT)
    parser.add_argument(
        "--update-env", action=argparse.BooleanOptionalAction, default=True
    )
    args = parser.parse_args()

    code = args.code or getpass.getpass("Authorization code: ").strip()
    client_secret = (
        args.client_secret or getpass.getpass("Zoho Client Secret: ").strip()
    )
    accounts_url = ZOHO_DATA_CENTERS[args.dc] if args.dc else args.accounts_url
    if not args.client_id:
        parser.error("--client-id is required or set ZOHO_CLIENT_ID in .env")
    if not client_secret:
        parser.error("--client-secret is required or set ZOHO_CLIENT_SECRET in .env")
    if not code:
        parser.error("--code is required")

    setup = ZohoSetupConfig(
        client_id=args.client_id,
        client_secret=client_secret,
        accounts_url=accounts_url,
        redirect_uri="" if args.self_client else args.redirect_uri,
        token_file=args.token_file,
    )
    token = await exchange_authorization_code(setup, code)
    save_token_cache(token, token_file=args.token_file)
    if args.update_env:
        update_env(
            {
                "ZOHO_CLIENT_ID": args.client_id,
                "ZOHO_CLIENT_SECRET": client_secret,
                "ZOHO_REFRESH_TOKEN": token.refresh_token,
                "ZOHO_ACCOUNTS_URL": accounts_url,
                "ZOHO_REDIRECT_URI": "" if args.self_client else args.redirect_uri,
                "ZOHO_MAIL_API_BASE": token.mail_api_base,
                "ZOHO_OAUTH_TOKEN_FILE": args.token_file,
            }
        )
    print("Token exchange OK. Refresh token saved; access token cached privately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
