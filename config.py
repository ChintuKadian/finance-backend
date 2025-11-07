# config.py
"""
Central config file.

IMPORTANT:
- Do NOT commit secrets (AWS access keys, DB passwords) to public repos.
- Prefer using IAM roles on EC2. If you must hardcode credentials temporarily,
  keep this file out of source control (add to .gitignore).
"""

from pathlib import Path

# App network
HOST = "0.0.0.0"
PORT = 5000

# AWS / resources (safe: bucket name, table names)
AWS_REGION = "us-east-1"
S3_BUCKET = "finance-tracker-store"          # <-- replace with your bucket name
TRANSACTIONS_TABLE = "Transactions"         # <-- replace with your table name

# Optional: override from environment automatically (useful if you later move to env vars)
import os
HOST = os.environ.get("HOST", HOST)
PORT = int(os.environ.get("PORT", PORT))
AWS_REGION = os.environ.get("AWS_REGION", AWS_REGION)
S3_BUCKET = os.environ.get("S3_BUCKET", S3_BUCKET)
TRANSACTIONS_TABLE = os.environ.get("TRANSACTIONS_TABLE", TRANSACTIONS_TABLE)

# --- Optional (NOT RECOMMENDED): Hardcoded AWS credentials (ONLY if absolutely necessary)
# If your EC2 instance already has an IAM role with permissions, leave these None.
# If you must hardcode for local testing (not recommended), set values and ensure config.py is .gitignored.
AWS_ACCESS_KEY_ID = None
AWS_SECRET_ACCESS_KEY = None
AWS_SESSION_TOKEN = None  # optional
# Example:
# AWS_ACCESS_KEY_ID = "AKIA..."
# AWS_SECRET_ACCESS_KEY = "xxxx"
