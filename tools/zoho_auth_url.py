#!/usr/bin/env python3
"""Generate a Zoho Mail OAuth authorization URL."""

from __future__ import annotations

import argparse

from zoho_setup_common import (
    TOKEN_PATH_DEFAULT,
    ZOHO_DATA_CENTERS,
    ZohoSetupConfig,
    build_authorization_url,
    config_from_env,
)


def main() -> int:
    env = config_from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default=env["client_id"])
    parser.add_argument("--accounts-url", default=env["accounts_url"])
    parser.add_argument(
        "--dc", choices=sorted(ZOHO_DATA_CENTERS), help="Zoho data center shortcut"
    )
    parser.add_argument("--redirect-uri", default=env["redirect_uri"])
    args = parser.parse_args()

    accounts_url = ZOHO_DATA_CENTERS[args.dc] if args.dc else args.accounts_url
    if not args.client_id:
        parser.error("--client-id is required or set ZOHO_CLIENT_ID in .env")
    config = ZohoSetupConfig(
        client_id=args.client_id,
        client_secret="",
        accounts_url=accounts_url,
        redirect_uri=args.redirect_uri,
        token_file=TOKEN_PATH_DEFAULT,
    )
    print(build_authorization_url(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
