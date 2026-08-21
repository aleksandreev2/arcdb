# ArchiveDB

Repository for the ArchiveDB web application and its Oracle Cloud deployment.

## Production baseline

Current deployment is understood as:

```text
Browser
  -> Cloudflare Worker / Cloudflare Tunnel
  -> Oracle Cloud Infrastructure (OCI) Ampere A1 instance
     - ARM
     - 4 OCPU
     - 24 GB RAM
     - OCI Block Volume for persistent storage
  -> Python / Flask application
  -> local files + JSON/CSV metadata + Telethon
```

This repository is intentionally being prepared before importing the live production source. The archive available during the initial audit may not be identical to the code currently running on Oracle, so the authoritative source should be copied from the live instance once SSH access is available.

## Important

Do **not** commit production data or credentials. In particular, keep these outside Git:

- Flask secret keys
- Telegram API credentials and `.session` files
- SMTP credentials
- MTProto proxy secrets
- user/account JSON files
- allowlists and access-control data
- EPUB files, covers and extracted chapters
- production logs and temporary packaging data

## Repository helpers

- `.env.example` — safe configuration reference
- `.gitignore` — excludes secrets, runtime state and large content
- `requirements.txt` — current Python runtime dependencies inferred from the source archive
- `docs/ARCHITECTURE.md` — current architecture notes and open questions
- `scripts/oracle_inventory.sh` — read-only inventory helper for the OCI instance

## Next step

After SSH access is available, run `scripts/oracle_inventory.sh`, identify the exact live application directory and service configuration, then import the production source into this repository before making performance changes.
