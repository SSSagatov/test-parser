"""Migrate a healthy PostgreSQL service, then execute the selected CLI command."""

import os
import subprocess
import sys

subprocess.run([sys.executable, "/app/main.py", "init-db"], check=True)
os.execv(sys.executable, [sys.executable, "/app/main.py", *sys.argv[1:]])
