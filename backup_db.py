"""
backup_db.py
============
Creates a consistent snapshot of data/app.db using SQLite's online backup
API — safe to run while the app is live (unlike a plain file copy, which
can grab a half-written page and produce a corrupt backup). Rotates old
backups automatically, keeping the most recent N.

Usage:
    python backup_db.py                # backup now, keep the last 14
    python backup_db.py --keep 30      # keep the last 30 instead

Run this on a schedule — see README.md "Deploying to production" for a
cron example (Linux/Mac) and a Windows Task Scheduler example.
"""
import argparse
import datetime
import glob
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "app.db")
BACKUP_DIR = os.path.join(BASE_DIR, "data", "backups")


def backup(keep=14):
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH} - nothing to back up.")
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(BACKUP_DIR, f"app_{timestamp}.db")

    src = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(dest_path)
    with dest:
        src.backup(dest)
    src.close()
    dest.close()
    print(f"Backed up to {dest_path} ({os.path.getsize(dest_path):,} bytes)")

    existing = sorted(glob.glob(os.path.join(BACKUP_DIR, "app_*.db")))
    to_delete = existing[:-keep] if len(existing) > keep else []
    for old in to_delete:
        os.remove(old)
        print(f"Removed old backup: {os.path.basename(old)}")

    return dest_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Back up data/app.db with rotation.")
    parser.add_argument("--keep", type=int, default=14, help="Number of recent backups to retain (default: 14)")
    args = parser.parse_args()
    backup(keep=args.keep)
