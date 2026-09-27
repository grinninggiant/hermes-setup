#!/bin/bash
# Daily second copy of the newest *verified* Honcho dump and per-profile state snapshots
# onto the encrypted "Hermes Backup" APFS volume (Seagate). Copies are re-attested on the target.
# Not off-host: the disk sits next to the Mac (fire/theft risk stays open).
# launchd runs it via ~/Applications/Hermes Backup.app so the Removable Volumes TCC grant
# is scoped to that app, not /bin/bash. Rebuild: osacompile -o "Hermes Backup.app" -e
# 'try' -e 'do shell script "/bin/bash <this script> >> <log> 2>> <err>"' -e 'end try';
# set CFBundleIdentifier com.grinninggiant.hermes.backup-external, LSUIElement, codesign -s -.
set -euo pipefail
umask 077
PATH="/usr/sbin:/usr/bin:/bin:/opt/homebrew/bin"

VOL_UUID="AC3419DC-1878-456C-957A-3809AFFEA7BA"
MNT="/Volumes/Hermes Backup"
KC_SVC="com.grinninggiant.hermes.backup-volume"
KC_ACC="seagate"
HERMES="/Users/mutlupolatcan/.hermes"
OPS="$HERMES/scripts/backup_ops.py"
SEND="$HERMES/scripts/hermes-send-keychain.sh"
PROFILES="general assistant coder finance health marketing producer researcher writer"

fail() {
  "$SEND" general --to telegram "⚠️ Harici disk yedeği başarısız: $1" >/dev/null 2>&1 || true
  echo "FAIL: $1" >&2
  exit 1
}

# Unlock/mount if needed; refuse anything that is not the expected encrypted volume.
if [ ! -d "$MNT" ]; then
  diskutil info "$VOL_UUID" >/dev/null 2>&1 || fail "Seagate bağlı değil"
  security find-generic-password -s "$KC_SVC" -a "$KC_ACC" -w | tr -d '\n' \
    | diskutil apfs unlockVolume "$VOL_UUID" -stdinpassphrase >/dev/null || fail "volume kilidi açılamadı"
fi
INFO="$(diskutil info "$MNT")" || fail "volume bilgisi okunamadı"
grep -q "$VOL_UUID" <<<"$INFO" || fail "beklenmeyen volume: $MNT"
grep -Eq 'FileVault: +Yes' <<<"$INFO" || fail "volume şifreli değil"

DAY="$(date +%Y%m%d)"
DEST="$MNT/sets/$DAY"
PART="$MNT/sets/.$DAY.partial"
rm -rf "$PART"
mkdir -p "$PART/honcho" "$PART/profiles"

# Honcho: newest dump that has a verified .meta.json.
DUMP="$(python3 - "$HERMES/backups/honcho" <<'PY'
from pathlib import Path
import json, sys
for p in sorted(Path(sys.argv[1]).glob("honcho-*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True):
    meta = p.with_name(p.name + ".meta.json")
    if meta.is_file() and json.loads(meta.read_text()).get("verified") is True:
        print(p); break
else:
    raise SystemExit(1)
PY
)" || fail "doğrulanmış Honcho dump yok"
cp -p "$DUMP" "$DUMP.sha256" "$PART/honcho/"
python3 "$OPS" attest "$PART/honcho/$(basename "$DUMP")" >/dev/null || fail "Honcho kopyası doğrulanamadı"

# Profiles: newest snapshot carrying retention-verified.json.
for p in $PROFILES; do
  SNAP="$(ls -1dt "$HERMES/profiles/$p/state-snapshots"/*/ 2>/dev/null | while read -r d; do
    [ -f "$d/retention-verified.json" ] && { echo "${d%/}"; break; }; done)"
  [ -n "$SNAP" ] || fail "$p için doğrulanmış snapshot yok"
  mkdir -p "$PART/profiles/$p"
  cp -Rp "$SNAP" "$PART/profiles/$p/"
  python3 "$OPS" attest "$PART/profiles/$p/$(basename "$SNAP")" >/dev/null || fail "$p kopyası doğrulanamadı"
done

rm -rf "$DEST"
mv "$PART" "$DEST"

# Retention: last 7 daily, last 4 Sundays, last 3 month-firsts.
python3 - "$MNT/sets" <<'PY'
from datetime import datetime
from pathlib import Path
import shutil, sys
root = Path(sys.argv[1])
sets = sorted((d for d in root.iterdir() if d.is_dir() and d.name.isdigit() and len(d.name) == 8), reverse=True)
dates = {d: datetime.strptime(d.name, "%Y%m%d") for d in sets}
keep = set(sets[:7])
keep |= set([d for d in sets if dates[d].weekday() == 6][:4])
keep |= set([d for d in sets if dates[d].day == 1][:3])
for d in sets:
    if d not in keep:
        shutil.rmtree(d)
PY

python3 -c 'import json,sys,time; print(json.dumps({"set":sys.argv[1],"completed_epoch":int(time.time())}))' "$DAY" > "$MNT/.last-success.json.partial"
mv "$MNT/.last-success.json.partial" "$MNT/last-success.json"
echo "ok set=$DAY honcho=$(basename "$DUMP") size=$(du -sh "$DEST" | cut -f1)"
