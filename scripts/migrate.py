"""Apply pending schema changes (Alembic). Run from deploy.sh before restarting services.

    python scripts/migrate.py            # upgrade to head
    python scripts/migrate.py downgrade -1
"""

import subprocess
import sys

args = sys.argv[1:] or ["upgrade", "head"]
sys.exit(subprocess.call([sys.executable, "-m", "alembic", *args]))
