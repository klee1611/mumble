"""Give memos from before sign-in existed to an account.

Rows without an owner are invisible to everyone. Sign in once on the app (that
creates your account), then:

    uv run python scripts/claim.py you@example.com
"""

import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent.parent))
from mumble import config  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    email = sys.argv[1].strip().lower()
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row) as conn, conn.transaction():
        acct = conn.execute("SELECT id FROM accounts WHERE email = %s", (email,)).fetchone()
        if not acct:
            sys.exit(f"no account for {email}; sign in on the app first")
        for table in ("memos", "items", "reviews"):
            n = conn.execute(f"UPDATE {table} SET account_id = %s WHERE account_id IS NULL",
                             (acct["id"],)).rowcount
            print(f"{table}: {n} claimed")


if __name__ == "__main__":
    main()
