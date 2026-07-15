#!/usr/bin/env python3
"""Interactive Zoho Mail setup for the job tracker bot."""

from __future__ import annotations

import asyncio
import getpass
import sys
import webbrowser

from zoho_setup_common import (
    ENV_PATH,
    TOKEN_PATH_DEFAULT,
    ZOHO_DATA_CENTERS,
    ZohoSetupConfig,
    build_authorization_url,
    exchange_authorization_code,
    get_accounts,
    get_folders,
    load_env,
    read_one_email_probe,
    save_token_cache,
    update_env,
)


def _ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    if secret and default:
        suffix = " [configured; press Enter to keep]"
    else:
        suffix = f" [{default}]" if default else ""
    full = f"{prompt}{suffix}: "
    value = getpass.getpass(full) if secret else input(full)
    return value.strip() or default


def _choose_accounts_url(default: str) -> str:
    print("\nZoho data center:")
    print("  eu   Europe          https://accounts.zoho.eu")
    print("  com  US/global       https://accounts.zoho.com")
    print("  in   India           https://accounts.zoho.in")
    print("  au   Australia       https://accounts.zoho.com.au")
    print("  jp   Japan           https://accounts.zoho.jp")
    print("  ca   Canada          https://accounts.zohocloud.ca")
    choice = _ask("Choose data center", "eu").lower()
    if choice in ZOHO_DATA_CENTERS:
        return ZOHO_DATA_CENTERS[choice]
    if choice.startswith("https://"):
        return choice.rstrip("/")
    return default.rstrip("/")


async def main() -> int:
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print(__doc__.strip())
        print()
        print("Usage:")
        print("  python tools/setup_zoho.py")
        print()
        print("The wizard asks for Zoho Client ID/Secret, opens the OAuth URL,")
        print("exchanges the pasted authorization code, updates .env, lists")
        print("folders, and verifies one email can be read without printing it.")
        return 0

    env = load_env()
    print("\nZoho Mail setup for jobs-tracker-bot")
    print("This will request read-only scopes only:")
    print("  ZohoMail.accounts.READ, ZohoMail.folders.READ, ZohoMail.messages.READ")
    print(f"It will update: {ENV_PATH}\n")

    client_id = _ask("Zoho Client ID", env.get("ZOHO_CLIENT_ID", ""))
    client_secret = _ask(
        "Zoho Client Secret",
        env.get("ZOHO_CLIENT_SECRET", ""),
        secret=True,
    )
    accounts_url = _choose_accounts_url(
        env.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.eu")
    )
    self_client_answer = _ask("Are you using Zoho Self Client?", "yes").lower()
    is_self_client = self_client_answer in {"y", "yes", "true", "1"}
    redirect_uri = ""
    if not is_self_client:
        redirect_uri = _ask(
            "OAuth redirect URI registered in Zoho API Console",
            env.get("ZOHO_REDIRECT_URI", "http://localhost"),
        )

    if not client_id or not client_secret:
        print("Client ID and Client Secret are required.", file=sys.stderr)
        return 2

    setup_config = ZohoSetupConfig(
        client_id=client_id,
        client_secret=client_secret,
        accounts_url=accounts_url,
        redirect_uri=redirect_uri,
        token_file=env.get("ZOHO_OAUTH_TOKEN_FILE", TOKEN_PATH_DEFAULT),
    )
    auth_url = build_authorization_url(setup_config)

    if is_self_client:
        print("\nIn Zoho API Console → Self Client → Generate Code:")
        print("Use these scopes:")
        print("  ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.READ")
        print("Then paste the generated grant code here.")
    else:
        print("\nOpening Zoho authorization URL in your browser...")
        print("If the browser does not open, copy this URL:")
        print(auth_url)
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        print("\nAfter approving, Zoho will redirect to your redirect URI.")
        print("Copy the `code` query parameter from the redirected URL.")
    code = _ask("Paste authorization code")
    if not code:
        print("Authorization code is required.", file=sys.stderr)
        return 2

    print("\nExchanging authorization code...")
    token = await exchange_authorization_code(setup_config, code)
    save_token_cache(token, token_file=setup_config.token_file)
    print(
        "Token exchange OK. Refresh token saved to .env; access token cached privately."
    )

    print("\nRetrieving Zoho account ID...")
    accounts = await get_accounts(
        access_token=token.access_token,
        mail_api_base=token.mail_api_base,
    )
    if not accounts:
        print("No Zoho Mail accounts were returned.", file=sys.stderr)
        return 1
    selected = accounts[0]
    if len(accounts) > 1:
        print("Accounts:")
        for idx, account in enumerate(accounts, 1):
            label = f"{account.account_id}"
            if account.email:
                label += f" ({account.email})"
            print(f"  {idx}. {label}")
        raw = _ask("Choose account number", "1")
        try:
            selected = accounts[max(0, min(len(accounts) - 1, int(raw) - 1))]
        except ValueError:
            selected = accounts[0]
    print(f"Account ID: {selected.account_id}")

    print("\nListing folders...")
    folders = await get_folders(
        access_token=token.access_token,
        mail_api_base=token.mail_api_base,
        account_id=selected.account_id,
    )
    if not folders:
        print("No folders returned.", file=sys.stderr)
        return 1
    print(f"Folders OK: {len(folders)} found")
    for folder in folders[:10]:
        print(f"  - {folder.name} ({folder.folder_id})")
    if len(folders) > 10:
        print(f"  ... {len(folders) - 10} more")

    print("\nTesting read of one email without printing its body...")
    probe = await read_one_email_probe(
        access_token=token.access_token,
        mail_api_base=token.mail_api_base,
        account_id=selected.account_id,
    )
    if probe is None:
        print(
            "No readable email found in available folders; OAuth/folders still verified."
        )
    else:
        print(
            "Email read OK: "
            f"folder={probe.folder_name!r}, message_id={probe.message_id}, "
            f"subject={probe.subject!r}, content_bytes={probe.content_length}"
        )

    update_env(
        {
            "ZOHO_MAIL_SYNC_ENABLED": env.get("ZOHO_MAIL_SYNC_ENABLED", "false"),
            "ZOHO_MAIL_SYNC_DRY_RUN": env.get("ZOHO_MAIL_SYNC_DRY_RUN", "true"),
            "ZOHO_CLIENT_ID": client_id,
            "ZOHO_CLIENT_SECRET": client_secret,
            "ZOHO_REFRESH_TOKEN": token.refresh_token,
            "ZOHO_ACCOUNT_ID": selected.account_id,
            "ZOHO_ACCOUNTS_URL": accounts_url,
            "ZOHO_REDIRECT_URI": redirect_uri,
            "ZOHO_MAIL_API_BASE": token.mail_api_base,
            "ZOHO_OAUTH_TOKEN_FILE": setup_config.token_file,
        }
    )
    print(f"\nSetup complete. Updated {ENV_PATH}.")
    print("Next safe test:")
    print("  python main.py --zoho-sync --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
