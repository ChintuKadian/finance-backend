# app.py - single-file backend for Finance Tracker (Flask + DynamoDB + S3 + Textract)
import os
import re
import uuid
import json
import datetime
from decimal import Decimal
from typing import Optional, Tuple

from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key, Attr

import config

app = Flask(__name__)
CORS(app)
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
s3 = boto3.client('s3', region_name='us-east-1')
transactions_table = dynamodb.Table('Transactions')
# ---------- Config / Table names ----------
AWS_REGION = getattr(config, "AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
TRANSACTIONS_TABLE = getattr(config, "TRANSACTIONS_TABLE", "Transactions")
BUDGETS_TABLE = getattr(config, "BUDGETS_TABLE", "UserBudgetsNew")
S3_BUCKET = getattr(config, "S3_BUCKET", os.environ.get("S3_BUCKET", None))
BUCKET_NAME = S3_BUCKET or "finance-tracker-store"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}

# ---------- AWS clients ----------
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)
textract = boto3.client("textract", region_name=AWS_REGION)

# Ensure table objects (these will raise at runtime if table names are wrong)
transactions_table = dynamodb.Table(TRANSACTIONS_TABLE)
budgets_table = dynamodb.Table(BUDGETS_TABLE)


# ---------- Utilities ----------
def decimal_to_float(obj):
    """Recursively convert DynamoDB Decimals to float for JSON serialization."""
    if isinstance(obj, list):
        return [decimal_to_float(i) for i in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---- S3 helpers ----
def upload_fileobj_to_s3(file_obj, bucket: str, key: str, content_type: str = None):
    """
    Upload a file-like object to S3. `file_obj` may be werkzeug FileStorage or a file-like stream.
    """
    # Read bytes from file-like object. Use .stream if present.
    try:
        if hasattr(file_obj, "stream"):
            body = file_obj.stream.read()
        else:
            body = file_obj.read()
        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type
        s3.put_object(Bucket=bucket, Key=key, Body=body, **(extra_args or {}))
        return True
    except ClientError as e:
        app.logger.exception("S3 upload failed: %s", e)
        raise


def make_s3_object_url(bucket: str, key: str) -> str:
    """Return a simple S3 URL. For private buckets you may want to use presigned URLs."""
    return f"https://{bucket}.s3.amazonaws.com/{key}"


# ---- Textract / parsing helpers (simple) ----
def extract_text_from_s3(bucket: str, key: str) -> str:
    """
    Calls Textract detect_document_text on an S3 object and returns concatenated text.
    Returns empty string on error.
    """
    try:
        resp = textract.detect_document_text(Document={"S3Object": {"Bucket": bucket, "Name": key}})
        lines = []
        for block in resp.get("Blocks", []):
            if block.get("BlockType") == "LINE":
                lines.append(block.get("Text", ""))
        return "\n".join(lines)
    except ClientError as e:
        app.logger.warning("Textract error: %s", e)
        return ""
    except Exception as e:
        app.logger.warning("Textract unknown error: %s", e)
        return ""


def parse_amount_and_date_from_text(text: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Simple heuristics to find the largest money-like number and common date patterns.
    Returns (amount, date_iso) where amount is float or None, date_iso is YYYY-MM-DD or None.
    """
    if not text:
        return None, None

    # Match numbers like 1,234.56 or 1234.56 or 1234
    amounts = re.findall(r"\b\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\b|\b\d+(?:\.\d{1,2})?\b", text)
    parsed_amounts = []
    for a in amounts:
        try:
            parsed_amounts.append(float(a.replace(",", "")))
        except Exception:
            continue
    amount = max(parsed_amounts) if parsed_amounts else None

    # Date patterns
    date_patterns = [
        r"(\d{4}-\d{2}-\d{2})",  # 2025-11-08
        r"(\d{2}/\d{2}/\d{4})",  # 08/11/2025 or 11/08/2025
        r"(\d{2}-\d{2}-\d{4})",  # 08-11-2025
        r"(\d{2}\.\d{2}\.\d{4})"  # 08.11.2025
    ]
    parsed_date = None
    for pat in date_patterns:
        m = re.search(pat, text)
        if not m:
            continue
        s = m.group(1)
        try:
            if "-" in s and len(s.split("-")[0]) == 4:
                parsed_date = s  # already YYYY-MM-DD
            elif "/" in s:
                # Try both common orders. We'll attempt day/month/year, then month/day/year.
                try:
                    dt = datetime.datetime.strptime(s, "%d/%m/%Y")
                    parsed_date = dt.date().isoformat()
                except Exception:
                    try:
                        dt = datetime.datetime.strptime(s, "%m/%d/%Y")
                        parsed_date = dt.date().isoformat()
                    except Exception:
                        parsed_date = None
            elif "-" in s:
                dt = datetime.datetime.strptime(s, "%d-%m-%Y")
                parsed_date = dt.date().isoformat()
            elif "." in s:
                dt = datetime.datetime.strptime(s, "%d.%m.%Y")
                parsed_date = dt.date().isoformat()
            if parsed_date:
                break
        except Exception:
            continue

    return amount, parsed_date


# ---- DynamoDB insert helper ----
def insert_transaction(table_name: str, tx_item: dict):
    """
    Insert a transaction into DynamoDB. Converts floats to Decimal.
    tx_item must contain at least: id, userId, amount, category, date, note, type, createdAt.
    """
    table = dynamodb.Table(table_name)
    item = dict(tx_item)
    item.setdefault("id", str(uuid.uuid4()))
    item.setdefault("createdAt", datetime.datetime.utcnow().isoformat() + "Z")
    # convert numeric fields to Decimal
    if "amount" in item:
        item["amount"] = Decimal(str(item["amount"]))
    try:
        table.put_item(Item=item)
    except ClientError as e:
        app.logger.exception("DynamoDB put_item failed: %s", e)
        raise


# ---------- Routes ----------
@app.route("/")
def home():
    return jsonify({"message": "Finance backend connected to DynamoDB!"})


# ---- Transactions: GET /transactions
@app.route("/transactions", methods=["GET"])
def get_transactions():
    try:
        resp = transactions_table.scan()
        items = resp.get("Items", [])
        return jsonify(decimal_to_float(items))
    except Exception as e:
        app.logger.exception("Failed to scan transactions")
        return jsonify({"error": str(e)}), 500


# ---- Transactions: POST /transactions
@app.route("/transactions", methods=["POST"])
def add_transaction():
    try:
        data = request.get_json() or {}
        tx_id = str(uuid.uuid4())
        item = {
            "id": tx_id,
            "transactionId": tx_id,
            "userId": data.get("userId", "default_user"),
            "amount": Decimal(str(data.get("amount", 0))),
            "category": data.get("category", "uncategorized"),
            "date": data.get("date", datetime.date.today().isoformat()),
            "note": data.get("note", ""),
            "type": data.get("type", "expense"),
            "createdAt": datetime.datetime.utcnow().isoformat() + "Z",
        }
        transactions_table.put_item(Item=item)
        return jsonify(decimal_to_float(item)), 201
    except Exception as e:
        app.logger.exception("Failed to add transaction")
        return jsonify({"error": str(e)}), 500


# ---- Summary ----
@app.route("/summary", methods=["GET"])
def get_summary():
    try:
        resp = transactions_table.scan()
        items = resp.get("Items", [])
        total_income = sum(float(t["amount"]) for t in items if t.get("type") == "income")
        total_expense = sum(float(t["amount"]) for t in items if t.get("type") == "expense")
        balance = total_income - total_expense
        return jsonify({"totalIncome": total_income, "totalExpenses": total_expense, "balance": balance})
    except Exception as e:
        app.logger.exception("Failed to compute summary")
        return jsonify({"error": str(e)}), 500

def _to_float_safe(v):
    try:
        return float(v)
    except Exception:
        return 0.0

#----------helper function
def _to_float_safe(v):
    try:
        return float(v)
    except Exception:
        return 0.0



# ---------- Budget endpoints (inline) ----------

current_budget = {"budgetLimit": 5000.0}

# ----------------------------
# 🟢 POST /budget → set budget
# ----------------------------
@app.route("/budget", methods=["POST"])
def set_budget():
    try:
        data = request.get_json()
        budget_limit = float(data.get("budgetLimit", 0))
        global current_budget
        current_budget["budgetLimit"] = budget_limit

        print(f"✅ Budget updated to: {budget_limit}")
        return jsonify({
            "message": "Budget updated successfully",
            "budgetLimit": budget_limit
        }), 200
    except Exception as e:
        print(f"❌ [POST /budget] Error: {e}")
        return jsonify({"error": str(e)}), 500


# ----------------------------
# 🟢 GET /budget → get budget + spent
# ----------------------------
@app.route("/budget", methods=["GET"])
def get_budget():
    try:
        print("🔍 Fetching all transactions to calculate total spent...")

        # Scan all transactions
        transactions_table = dynamodb.Table(TRANSACTIONS_TABLE)
        response = transactions_table.scan(
            ProjectionExpression="amount"
            )
        items = response.get("Items", [])

        total_spent = sum(float(item.get("amount", 0)) for item in items)

        # Handle pagination
        while "LastEvaluatedKey" in response:
            response = transactions_table.scan(
                ProjectionExpression="amount",
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            total_spent += sum(float(item.get("amount", 0)) for item in response.get("Items", []))

        print(f"✅ Total spent: {total_spent}")

        # Get stored budget (default = ₹5000)
        budget_limit = float(current_budget.get("budgetLimit", 5000))
        remaining = budget_limit - total_spent

        return jsonify({
            "month": "2025-11",  # Fixed month
            "budgetLimit": budget_limit,
            "spent": total_spent,
            "remaining": remaining
        }), 200

    except Exception as e:
        print(f"❌ [GET /budget] Error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------- Upload endpoint (uses the inline helpers above) ----------

@app.route("/export-data", methods=["GET"])
def export_data_to_s3():
    try:
        print("🟢 Starting export process...")

        # Initialize S3 and DynamoDB
        s3 = boto3.client("s3", region_name="us-east-1")
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        transactions_table = dynamodb.Table("Transactions")  # ⚠️ Change if your table name differs

        # 1️⃣ Fetch all transactions
        print("📥 Fetching data from DynamoDB...")
        response = transactions_table.scan()
        items = response.get("Items", [])
        print(f"✅ Retrieved {len(items)} transactions from DynamoDB.")

        # Handle pagination if there are more items
        while "LastEvaluatedKey" in response:
            response = transactions_table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
            print(f"📄 Retrieved additional {len(response.get('Items', []))} items... Total now: {len(items)}")

        # 2️⃣ Convert to JSON
        print("🔄 Converting items to JSON format...")
        json_data = json.dumps(items, indent=2, default=str)
        print("✅ JSON conversion successful. Sample preview:")
        print(json_data[:300] + "..." if len(json_data) > 300 else json_data)

        # 3️⃣ Upload to S3
        bucket_name = "finance-tracker-store"  # ⚠️ Replace with your actual S3 bucket name
        file_name = "transactions_export.json"
        print(f"🚀 Uploading data to S3 bucket: {bucket_name} as {file_name} ...")

        s3.put_object(
            Bucket=bucket_name,
            Key=file_name,
            Body=json_data,
            ContentType="application/json"
        )

        print("✅ Successfully uploaded transactions_export.json to S3.")
        return jsonify({"message": "Data exported successfully to S3", "total_items": len(items)}), 200

    except ClientError as e:
        print("❌ AWS ClientError:", e)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        print("❌ Unexpected Error:", e)
        return jsonify({"error": str(e)}), 500



@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.datetime.utcnow().isoformat() + "Z"}), 200


if __name__ == "__main__":
    host = getattr(config, "HOST", "0.0.0.0")
    port = int(getattr(config, "PORT", 5000))
    app.run(host=host, port=port, debug=True)
