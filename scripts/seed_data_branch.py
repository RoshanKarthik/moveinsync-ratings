"""Upload the local review database to the repository's `data` branch, once, before the first
scheduled run. Does exactly what the GitHub workflow does after each run.

    uv run python scripts/seed_data_branch.py            # pushes to the `origin` remote
    uv run python scripts/seed_data_branch.py --remote upstream

The full history takes hours to tag on the free model tier, far longer than a CI run may last,
so CI starts from this copy and only tags what is new. Your project folder and its git history
are not touched: the push is made from a temporary folder.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--remote", default="origin")
    args = ap.parse_args()

    db = ROOT / "data" / "ratings.db"
    if not db.exists():
        print(f"No database at {db}. Run `uv run ratings refresh` first.")
        return 1
    try:
        url = git("remote", "get-url", args.remote, cwd=ROOT)
    except subprocess.CalledProcessError:
        print(f"This folder has no git remote '{args.remote}'. Push the project to GitHub first.")
        return 1

    # ignore_cleanup_errors: git marks its object files read-only, which Windows refuses to delete.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        # A consistent copy even if the database is in WAL mode.
        src, dst = sqlite3.connect(db), sqlite3.connect(work / "ratings.db")
        try:
            src.backup(dst)
            n = dst.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        finally:
            src.close()
            dst.close()
        git("init", "-q", "-b", "data", cwd=work)
        git("add", "ratings.db", cwd=work)
        git(
            "-c",
            "user.name=ratings-bot",
            "-c",
            "user.email=ratings-bot@users.noreply.github.com",
            "commit",
            "-q",
            "-m",
            "data: seed from local database",
            cwd=work,
        )
        size = (work / "ratings.db").stat().st_size / 1e6
        print(f"Pushing {n:,} reviews ({size:.0f} MB) to the 'data' branch of {url} ...")
        git("push", "-q", "--force", url, "data", cwd=work)
    print("Done. Now run the workflow: GitHub > Actions > Refresh ratings report > Run workflow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
