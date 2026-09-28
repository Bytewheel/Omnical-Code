#!/usr/bin/env python3
"""Build the combined Omnical Apple configuration profile (.mobileconfig) for iOS.

Per PLAN.md Phase 7 (iPhone): one combined profile with
  - 7 x CalDAV  payloads  -> https://<HOST>:<PORT>/caldav-compat/principal/<user>
  - 7 x CardDAV payloads  -> https://<HOST>:<PORT>/carddav/principal/<user>
  - 1 x family CalDAV payload via user$group impersonation (primary identity)

Unlike the stock RustiCal per-identity profile (which embeds the Host header
including the port into CalDAVHostName and repeats one family payload per
identity), this uses a bare *HostName + explicit *Port + UseSSL + full
PrincipalURLs, and contains the family payload exactly once.

Re-run safe: all UUIDs are deterministic (uuid5), so re-generating and
re-installing the profile replaces the accounts instead of duplicating them.

Passwords (app tokens) are read from `pass` and written ONLY to the output
file, which is created with mode 0600. Never print or commit them.
"""
import plistlib
import subprocess
import uuid
from pathlib import Path

HOST = "0115d8cf.duckdns.org"
PORT = 8443
PRIMARY = "burningserenity@gmail.com"  # identity used for the family impersonation
FAMILY = "family"
IDENTITIES = [
    "burningserenity@gmail.com",
    "nfcarlton@gmail.com",
    "nfcalaway@gmail.com",
    "nicholas@hawksnestsoftware.com",
    "nicholas@carltonaudio.com",
    "nfcalaway@novo-ordo.com",
    "zero@novo-ordo.com",
]
PASS_PREFIX = "secrets/omnical"
OUT = Path(__file__).resolve().parent.parent / "out" / "omnical-iphone.mobileconfig"


def token(identity: str, client: str = "apple") -> str:
    r = subprocess.run(
        ["pass", "show", f"{PASS_PREFIX}/{identity}/{client}"],
        capture_output=True, text=True, check=True,
    )
    tok = r.stdout.strip()
    if not tok:
        raise SystemExit(f"empty token for {identity}/{client}")
    return tok


def payload_uuid(kind: str, ident: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://{HOST}:{PORT}/omnical-profile/{kind}/{ident}"))


def caldav_payload(description, username, password, principal_path, ident_key):
    return {
        "PayloadType": "com.apple.caldav.account",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"org.omnical.iphone.caldav.{ident_key}",
        "PayloadUUID": payload_uuid("caldav", ident_key),
        "PayloadDisplayName": f"Omnical Calendar ({description})",
        "PayloadDescription": "Omnical CalDAV account",
        "PayloadOrganization": "Omnical",
        "CalDAVAccountDescription": f"Omnical - {description}",
        "CalDAVHostName": HOST,
        "CalDAVPort": PORT,
        "CalDAVUseSSL": True,
        "CalDAVUsername": username,
        "CalDAVPassword": password,
        "CalDAVPrincipalURL": f"https://{HOST}:{PORT}{principal_path}",
    }


def carddav_payload(description, username, password, principal_path, ident_key):
    return {
        "PayloadType": "com.apple.carddav.account",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"org.omnical.iphone.carddav.{ident_key}",
        "PayloadUUID": payload_uuid("carddav", ident_key),
        "PayloadDisplayName": f"Omnical Contacts ({description})",
        "PayloadDescription": "Omnical CardDAV account",
        "PayloadOrganization": "Omnical",
        "CardDAVAccountDescription": f"Omnical - {description}",
        "CardDAVHostName": HOST,
        "CardDAVPort": PORT,
        "CardDAVUseSSL": True,
        "CardDAVUsername": username,
        "CardDAVPassword": password,
        "CardDAVPrincipalURL": f"https://{HOST}:{PORT}{principal_path}",
    }


def main():
    payloads = []
    for ident in IDENTITIES:
        tok = token(ident)
        payloads.append(caldav_payload(
            ident, ident, tok, f"/caldav-compat/principal/{ident}", ident))
        payloads.append(carddav_payload(
            ident, ident, tok, f"/carddav/principal/{ident}", ident))
    # family via impersonation (CalDAV only, like the stock template's membership payload)
    payloads.append(caldav_payload(
        FAMILY, f"{PRIMARY}${FAMILY}", token(PRIMARY),
        f"/caldav-compat/principal/{FAMILY}", f"{FAMILY}.impersonated"))

    profile = {
        "PayloadContent": payloads,
        "PayloadDisplayName": "Omnical",
        "PayloadDescription": (
            f"Omnical CalDAV/CardDAV accounts ({len(IDENTITIES)} identities + {FAMILY}) "
            f"on {HOST}:{PORT}"
        ),
        "PayloadIdentifier": "org.omnical.iphone",
        "PayloadOrganization": "Omnical",
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": payload_uuid("profile", "toplevel"),
        "PayloadVersion": 1,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "wb") as f:
        plistlib.dump(profile, f, fmt=plistlib.FMT_XML, sort_keys=True)
    OUT.chmod(0o600)

    # sanity round-trip: no tokens echoed
    with open(OUT, "rb") as f:
        check = plistlib.load(f)
    kinds = {}
    for p in check["PayloadContent"]:
        kinds[p["PayloadType"]] = kinds.get(p["PayloadType"], 0) + 1
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, mode {oct(OUT.stat().st_mode & 0o777)})")
    print(f"payloads: {sum(kinds.values())} total = {kinds}")


if __name__ == "__main__":
    main()
