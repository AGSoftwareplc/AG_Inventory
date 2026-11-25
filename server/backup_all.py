"""Back up all AG Inventory data: every business database, the platform file, logos and the secret key.

Safe while the app is running: databases are copied with SQLite's online backup.
Each run makes one folder, backups\\server\\YYYYMMDD-HHMMSS, and keeps the newest copies (30 by default).

    .venv\\Scripts\\python server\\backup_all.py                     (run from the AG_Inventory folder)
    .venv\\Scripts\\python server\\backup_all.py --copy-to E:\\AG_Backups --keep 30
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def backup(data_dir, dest_root, keep):
    dest = os.path.join(dest_root, datetime.now().strftime('%Y%m%d-%H%M%S'))
    count = 0
    for folder, _dirs, files in os.walk(data_dir):
        out_dir = os.path.join(dest, os.path.relpath(folder, data_dir))
        os.makedirs(out_dir, exist_ok=True)
        for name in files:
            src = os.path.join(folder, name)
            if name.endswith(('.db-wal', '.db-shm')):
                continue  # included in the .db copy below
            if name.endswith('.db'):
                con = sqlite3.connect(src, timeout=30)
                out = sqlite3.connect(os.path.join(out_dir, name))
                con.backup(out)
                out.close()
                con.close()
            else:
                shutil.copy2(src, out_dir)
            count += 1
    # keep only the newest copies
    runs = sorted(d for d in os.listdir(dest_root) if os.path.isdir(os.path.join(dest_root, d)) and d[:8].isdigit())
    for old in runs[:-keep]:
        shutil.rmtree(os.path.join(dest_root, old), ignore_errors=True)
    return dest, count


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--data', default=os.path.join(ROOT, 'data'), help='the data folder (default: AG_Inventory\\data)')
    p.add_argument('--dest', default=os.path.join(ROOT, 'backups', 'server'), help='where to keep the backups')
    p.add_argument('--copy-to', help='also keep copies here, for example a second disk or a network share')
    p.add_argument('--keep', type=int, default=30, help='how many backups to keep in each place (default 30)')
    a = p.parse_args()
    if not os.path.isdir(a.data):
        sys.exit(f'Data folder not found: {a.data}')
    keep = max(1, a.keep)
    for dest_root in [a.dest] + ([a.copy_to] if a.copy_to else []):
        os.makedirs(dest_root, exist_ok=True)
        dest, count = backup(a.data, dest_root, keep)
        print(f'{datetime.now():%Y-%m-%d %H:%M:%S}  Backed up {count} files to {dest}')


if __name__ == '__main__':
    main()
