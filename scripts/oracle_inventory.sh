#!/usr/bin/env bash
set -u

section() { printf '\n===== %s =====\n' "$1"; }

section "SYSTEM"
uname -a || true
printf '\n'; lscpu 2>/dev/null | sed -n '1,30p' || true
printf '\n'; free -h || true

section "BLOCK DEVICES"
lsblk -o NAME,SIZE,TYPE,FSTYPE,FSVER,MOUNTPOINTS,MODEL 2>/dev/null || lsblk || true

section "FILESYSTEMS"
df -hT || true

section "INODES"
df -hi || true

section "MOUNTS"
findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null || mount || true

section "HOME DIRECTORY SIZES"
du -xhd1 /home/ubuntu 2>/dev/null | sort -h || true

section "LIKELY ARCHIVEDB DIRECTORIES"
find /home/ubuntu -maxdepth 3 -type d \
  \( -iname '*archive*' -o -iname '*novel*' -o -iname '*epub*' -o -iname '*metadata*' -o -iname '*structured*' \) \
  -print 2>/dev/null | sort | head -n 300 || true

section "WEB / TUNNEL PROCESSES"
ps -eo user,pid,ppid,%cpu,%mem,etime,comm | \
  grep -Ei 'gallery_app|flask|gunicorn|python|cloudflared|nginx' | grep -v grep || true

section "RELEVANT SYSTEMD SERVICES"
systemctl list-units --type=service --all --no-pager 2>/dev/null | \
  grep -Ei 'archive|novel|gallery|flask|gunicorn|python|cloudflared|nginx' || true

section "PYTHON"
python3 --version 2>/dev/null || true
python3 -m pip --version 2>/dev/null || true

section "LISTENING PORTS"
ss -lntp 2>/dev/null || true

section "DONE"
echo "Inventory complete. Command lines and block-device serials were intentionally omitted."
echo "Review paths and infrastructure details before sharing the output publicly."
