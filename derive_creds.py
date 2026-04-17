"""
One-shot helper — derives Polymarket L2 API credentials from a private key
and writes them into .env. Delete this file after use.

Run interactively:
    python3 derive_creds.py
"""

from __future__ import annotations

import getpass
import os
import pathlib
import re
import sys

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

HOST = "https://clob.polymarket.com"
ENV_PATH = pathlib.Path(__file__).resolve().parent / ".env"


def mask(s: str, keep: int = 4) -> str:
    if not s:
        return "<empty>"
    if len(s) <= keep * 2:
        return "*" * len(s)
    return s[:keep] + "…" + s[-keep:]


def upsert_env(lines: list[str], key: str, value: str) -> list[str]:
    pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=.*$")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            return lines
    lines.append(f"{key}={value}")
    return lines


def main() -> int:
    if not ENV_PATH.exists():
        print(f"ERROR: {ENV_PATH} not found. Run: cp .env.example .env", file=sys.stderr)
        return 1

    print("Paste your Polygon private key (input hidden, no echo).")
    print("Format: 64 hex characters, with or without a leading 0x.")
    raw = getpass.getpass("PRIVATE_KEY: ").strip()
    # MetaMask exports 64 hex chars without 0x — accept both.
    pk = raw if raw.startswith("0x") else "0x" + raw

    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", pk):
        hex_only = pk[2:] if pk.startswith("0x") else pk
        print("ERROR: that doesn't look like a valid private key.", file=sys.stderr)
        print(f"  Got {len(hex_only)} hex chars; need exactly 64.", file=sys.stderr)
        print("  Make sure you copied the full key and didn't grab the seed phrase by mistake.", file=sys.stderr)
        return 1

    print("\nConnecting to Polymarket CLOB and deriving API credentials …")
    client = ClobClient(HOST, key=pk, chain_id=POLYGON)

    try:
        creds = client.create_or_derive_api_creds()
    except Exception as exc:
        print(f"ERROR deriving creds: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Common causes: wallet has never interacted with Polymarket,", file=sys.stderr)
        print("network issue, or invalid key. Try signing into polymarket.com", file=sys.stderr)
        print("with this wallet first to register it, then rerun.", file=sys.stderr)
        return 1

    lines = ENV_PATH.read_text().splitlines()
    lines = upsert_env(lines, "POLYMARKET_PRIVATE_KEY", pk)
    lines = upsert_env(lines, "POLYMARKET_API_KEY", creds.api_key)
    lines = upsert_env(lines, "POLYMARKET_API_SECRET", creds.api_secret)
    lines = upsert_env(lines, "POLYMARKET_API_PASSPHRASE", creds.api_passphrase)
    ENV_PATH.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)

    print("\nWrote to .env (perms 0600):")
    print(f"  POLYMARKET_PRIVATE_KEY    = {mask(pk)}")
    print(f"  POLYMARKET_API_KEY        = {mask(creds.api_key)}")
    print(f"  POLYMARKET_API_SECRET     = {mask(creds.api_secret)}")
    print(f"  POLYMARKET_API_PASSPHRASE = {mask(creds.api_passphrase)}")
    print("\nDone. You can now delete this helper: rm derive_creds.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
