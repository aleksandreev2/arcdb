# Current architecture baseline

## Confirmed

- Application: Python + Flask.
- Telegram integration: Telethon background client.
- Production compute: OCI Ampere A1 ARM instance, 4 OCPU / 24 GB RAM.
- Persistent storage: OCI Block Volume.
- Public edge: Cloudflare (`workers.dev`) with Cloudflare-aware origin code.
- The source archive stores important mutable state in JSON/CSV files and local directories.

## Important source-level constraints

The archived application is designed as a single-process monolith. Several locks, caches, rate-limit buckets and the Telegram client are process-local. Do not increase WSGI process count until those components are reviewed or moved to shared persistence.

Large EPUB operations and filesystem scans also happen inside application code. Their real impact depends on the Block Volume mount, performance tier, data size and current process manager.

## Still to inventory on the OCI instance

- Boot Volume size and filesystem.
- Block Volume size, performance tier (VPU/GB), attachment type and mount point.
- Filesystem type and mount options.
- Actual paths occupied by ArchiveDB data.
- Current systemd services/process manager.
- Cloudflared configuration and routing.
- Python version and installed package versions.
- Current disk utilization, inode utilization and I/O pressure.
- Backup/snapshot strategy.

Use `scripts/oracle_inventory.sh` for the first read-only pass.
