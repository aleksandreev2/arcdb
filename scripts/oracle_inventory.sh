#!/usr/bin/env bash
set -u

search_roots=()

usage() {
  cat <<'EOF'
Usage: bash scripts/oracle_inventory.sh [--search-root /explicit/root]...

Runs a read-only discovery pass. It does not guess an ArchiveDB application or
data root. Add each operator-approved absolute filesystem root that should be
searched for state/storage candidates. Redirect output to a private file.

The output contains production paths and service user names. Keep it private.
No file contents, environment values, process command lines, device serials or
cloudflared credential values are collected.
EOF
}

while (($#)); do
  case "$1" in
    --search-root)
      if (($# < 2)); then
        echo "Missing value for --search-root." >&2
        exit 2
      fi
      if [[ "$2" != /* ]]; then
        echo "Search roots must be explicit absolute paths." >&2
        exit 2
      fi
      search_roots+=("$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

section() { printf '\n===== %s =====\n' "$1"; }

section "SAFETY"
echo "Read-only discovery pass. Treat this complete output as private."
echo "File contents, environment values, command lines and device serials are omitted."
if ((${#search_roots[@]} == 0)); then
  echo "No filesystem search roots supplied; candidate-file discovery will be skipped."
fi

section "SYSTEM"
uname -a || true
printf '\n'; lscpu 2>/dev/null | sed -n '1,30p' || true
printf '\n'; free -h || true
python3 --version 2>/dev/null || true
python3 -m pip --version 2>/dev/null || true

section "BLOCK DEVICES"
lsblk -o NAME,SIZE,TYPE,FSTYPE,FSVER,MOUNTPOINTS,MODEL 2>/dev/null || lsblk || true

section "FILESYSTEMS AND INODES"
df -hT || true
printf '\n'; df -hi || true

section "MOUNTS"
findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null || mount || true

section "RELEVANT PROCESS EXECUTABLES AND WORKING DIRECTORIES"
while read -r pid command; do
  case "${command,,}" in
    *archive*|*gallery*|*flask*|*gunicorn*|python*|cloudflared|nginx)
      executable=$(readlink "/proc/$pid/exe" 2>/dev/null || true)
      working_directory=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
      printf 'pid=%s command=%s executable=%s working_directory=%s\n' \
        "$pid" "$command" "${executable:-unavailable}" "${working_directory:-unavailable}"
      ;;
  esac
done < <(ps -eo pid=,comm= 2>/dev/null || true)

section "RELEVANT SYSTEMD SERVICES"
relevant_units=$(
  systemctl list-unit-files --type=service --no-legend --no-pager 2>/dev/null |
    awk '{print $1}' |
    grep -Ei 'archive|novel|gallery|flask|gunicorn|python|cloudflared|nginx' || true
)
if [[ -z "$relevant_units" ]]; then
  echo "No matching unit files discovered or systemd unavailable."
else
  while read -r unit; do
    [[ -n "$unit" ]] || continue
    systemctl show "$unit" --no-pager \
      --property=Id,LoadState,ActiveState,SubState,FragmentPath,DropInPaths,User,Group,WorkingDirectory,EnvironmentFiles \
      2>/dev/null || true
    printf '\n'
  done <<< "$relevant_units"
fi

section "CLOUDFLARED CONFIG FILE CANDIDATES"
for directory in /etc/cloudflared /usr/local/etc/cloudflared; do
  if [[ -d "$directory" ]]; then
    find "$directory" -maxdepth 2 -type f -printf '%p bytes=%s\n' 2>/dev/null | sort || true
  fi
done
echo "Config contents and credential values were intentionally omitted."

section "LISTENING PORTS"
ss -lntp 2>/dev/null || true

section "EXPLICIT SEARCH ROOTS"
for root in "${search_roots[@]}"; do
  if [[ ! -d "$root" ]]; then
    printf 'missing root=%s\n' "$root"
    continue
  fi
  printf 'root=%s\n' "$root"
  du -xhd1 "$root" 2>/dev/null | sort -h || true
done

section "ARCHIVEDB STATE AND STORAGE CANDIDATES"
for root in "${search_roots[@]}"; do
  [[ -d "$root" ]] || continue
  find "$root" -xdev -maxdepth 6 \
    \( \
      -type f \( \
        -name 'gallery_app.py' -o -name 'users.json' -o -name 'user_data.json' -o \
        -name 'collections.json' -o -name 'user_uploads.json' -o \
        -name 'custom_meta.json' -o -name 'allowed_gmails.txt' -o \
        -name 'uploaded_novels_tracker.csv' -o -name 'master_library_index.csv' -o \
        -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.service' \
      \) -o \
      -type d \( \
        -iname '*archive*' -o -iname '*novel*' -o -iname '*epub*' -o \
        -iname '*metadata*' -o -iname '*structured*' -o -iname '*chapter*' \
      \) \
    \) -printf '%y %p bytes=%s\n' 2>/dev/null | sort | head -n 2000 || true
done

section "SOURCE REVISIONS FOR DISCOVERED PROCESS DIRECTORIES"
declare -A checked_directories=()
while read -r pid command; do
  case "${command,,}" in
    *archive*|*gallery*|*flask*|*gunicorn*|python*)
      working_directory=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
      [[ -n "$working_directory" ]] || continue
      [[ -z "${checked_directories[$working_directory]:-}" ]] || continue
      checked_directories[$working_directory]=1
      if git -C "$working_directory" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        revision=$(git -C "$working_directory" rev-parse HEAD 2>/dev/null || true)
        changes=$(git -C "$working_directory" status --porcelain --untracked-files=no 2>/dev/null | wc -l)
        printf 'working_directory=%s revision=%s tracked_changes=%s\n' \
          "$working_directory" "${revision:-unavailable}" "$changes"
      else
        printf 'working_directory=%s revision=not-a-git-worktree\n' "$working_directory"
      fi
      ;;
  esac
done < <(ps -eo pid=,comm= 2>/dev/null || true)

section "NEXT STEP"
echo "Review this private discovery output and identify explicit application, metadata, SQLite, content, unit and cloudflared-config paths."
echo "Then run scripts/collect_production_inventory.py with those explicit paths."
echo "Do not share this raw discovery output publicly."
