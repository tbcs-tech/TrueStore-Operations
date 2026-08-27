"""
backup_offsite.py
=================
Push local database backups to an offsite destination so a disk failure
doesn't take out both the live DB and its backups.

Supports three transport modes (auto-detected from the destination string):

  1. SCP — remote server via SSH
     python backup_offsite.py scp user@host:/path/to/backups/
     Uses the system `scp` command (SSH key auth recommended).

  2. rclone — any rclone remote (S3, Google Drive, Backblaze, Dropbox, etc.)
     python backup_offsite.py rclone myremote:bucket/truestore-backups
     Requires rclone configured (`rclone config`) with the named remote.

  3. local — another mount point / USB drive / NAS share
     python backup_offsite.py local /mnt/nas/truestore-backups/

Also does a fresh local backup first (via backup_db.py) so the offsite
copy is always current.

Cron example (daily at 2:30am, after the 2am local backup):
  30 2 * * * cd /opt/truestore/receivables_dashboard && .venv/bin/python backup_offsite.py rclone myremote:truestore-backups >> logs/offsite_backup.log 2>&1

All output goes to stdout for logging. Exit code 0 = success, 1 = failure.
"""
import argparse
import os
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, "data", "backups")
DB_PATH = os.path.join(BASE_DIR, "data", "app.db")


def log(msg):
    from datetime import datetime
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fresh_local_backup():
    """Run a fresh local backup first, so we're shipping the latest snapshot."""
    try:
        import backup_db
        path = backup_db.backup(keep=30)
        if path:
            log(f"Fresh local backup: {os.path.basename(path)}")
            return path
        log("WARNING: local backup returned None (no DB found?)")
        return None
    except Exception as e:
        log(f"WARNING: local backup failed: {e}")
        return None


def push_scp(dest):
    """Push the entire backups directory to a remote host via scp."""
    if not os.path.isdir(BACKUP_DIR):
        log("No backups directory to push.")
        return False
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    if not files:
        log("No backup files to push.")
        return False
    # Push only the latest backup + the live DB
    latest = os.path.join(BACKUP_DIR, files[-1])
    cmd = ["scp", "-o", "StrictHostKeyChecking=accept-new", latest, dest]
    log(f"SCP: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"SCP FAILED: {result.stderr.strip()}")
        return False
    log(f"SCP OK: {files[-1]} → {dest}")
    return True


def push_rclone(dest):
    """Sync the backups directory to an rclone remote."""
    if not shutil.which("rclone"):
        log("ERROR: rclone not found in PATH. Install: https://rclone.org/install/")
        return False
    if not os.path.isdir(BACKUP_DIR):
        log("No backups directory to push.")
        return False
    cmd = ["rclone", "sync", BACKUP_DIR, dest,
           "--progress", "--transfers", "2", "--log-level", "NOTICE"]
    log(f"rclone: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"rclone FAILED: {result.stderr.strip()}")
        return False
    log(f"rclone OK: synced to {dest}")
    return True


def push_local(dest):
    """Copy backups to another local/mounted path."""
    if not os.path.isdir(BACKUP_DIR):
        log("No backups directory to push.")
        return False
    os.makedirs(dest, exist_ok=True)
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    count = 0
    for f in files:
        src = os.path.join(BACKUP_DIR, f)
        dst = os.path.join(dest, f)
        if not os.path.exists(dst) or os.path.getsize(src) != os.path.getsize(dst):
            shutil.copy2(src, dst)
            count += 1
    log(f"Local copy: {count} new/updated file(s) → {dest}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Push TrueStore DB backups offsite.",
        epilog="Examples:\n"
               "  python backup_offsite.py scp deploy@backup-host:/backups/truestore/\n"
               "  python backup_offsite.py rclone s3remote:mybucket/truestore\n"
               "  python backup_offsite.py local /mnt/usb/truestore-backups/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("mode", choices=["scp", "rclone", "local"],
                        help="Transport mode")
    parser.add_argument("destination",
                        help="Where to push (scp: user@host:/path, rclone: remote:path, local: /path)")
    parser.add_argument("--skip-local", action="store_true",
                        help="Skip creating a fresh local backup first")
    args = parser.parse_args()

    log("=== Offsite backup start ===")

    if not args.skip_local:
        fresh_local_backup()

    ok = False
    if args.mode == "scp":
        ok = push_scp(args.destination)
    elif args.mode == "rclone":
        ok = push_rclone(args.destination)
    elif args.mode == "local":
        ok = push_local(args.destination)

    if ok:
        log("=== Offsite backup complete ===")
        sys.exit(0)
    else:
        log("=== Offsite backup FAILED ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
