#!/usr/bin/env bash
set -euo pipefail

# GitHub-hosted Ubuntu runner mirrors occasionally stop resolving. Keep the
# default signed Ubuntu sources while DNS works. Switch to an independent
# HTTPS mirror only when the default archive or security DNS fails.
if getent ahostsv4 azure.archive.ubuntu.com >/dev/null 2>&1 \
  && getent ahostsv4 security.ubuntu.com >/dev/null 2>&1; then
  echo 'Default Ubuntu mirrors resolve; retaining runner apt sources.'
  exit 0
fi

if ! getent ahostsv4 mirrors.kernel.org >/dev/null 2>&1; then
  echo '::error::Ubuntu DNS failed and the independent mirror does not resolve.' >&2
  exit 1
fi

echo '::warning::Ubuntu archive DNS failed; using mirrors.kernel.org with Ubuntu signature checks unchanged.'
shopt -s nullglob
sources=(/etc/apt/apt-mirrors.txt /etc/apt/sources.list \
         /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list)
changed=0
for source in "${sources[@]}"; do
  [[ -f "$source" ]] || continue
  if grep -Eq 'https?://(azure\.archive|archive|security)\.ubuntu\.com/ubuntu' "$source"; then
    sudo sed -i -E \
      's#https?://(azure\.archive|archive|security)\.ubuntu\.com/ubuntu#https://mirrors.kernel.org/ubuntu#g' \
      "$source"
    changed=$((changed + 1))
  fi
done

if [[ "$changed" -eq 0 ]]; then
  echo '::error::Could not locate recognized Ubuntu apt mirror URLs; refusing fallback.' >&2
  exit 1
fi

echo "Switched $changed apt source file(s); package signatures remain enforced."
