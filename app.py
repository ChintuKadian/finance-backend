# app.py - single-file backend for Finance Tracker (Flask + DynamoDB + S3 + Textract)
# app.py - cleaned single-file backend for Finance Tracker (Flask + DynamoDB + optional S3/Textract + OCR)
import os
import io
import re
import uuid
import json
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional, Dict, Any

from dateutil.parser import parse as parse_date
from PIL import Image, ImageOps, ImageFilter
import pytesseract

from flask import Flask, request, jsonify, current_app
from flask_cors import CORS
from werkzeug.utils import secure_filename

import boto3
from botocore.exceptions import ClientError

import config  # your local config module (optional)

# -------------------- App / config --------------------
app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

AWS_REGION = getattr(config, "AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
TRANSACTIONS_TABLE = getattr(config, "TRANSACTIONS_TABLE", os.environ.get("TRANSACTIONS_TABLE", "Transactions"))
BUDGETS_TABLE = getattr(config, "BUDGETS_TABLE", os.environ.get("BUDGETS_TABLE", "UserBudgetsNew"))
S3_BUCKET = getattr(config, "S3_BUCKET", os.environ.get("S3_BUCKET", None))
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}

# -------------------- AWS clients --------------------
# boto3 will use instance role, env vars, or configured credentials
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)
textract = boto3.client("textract", region_name=AWS_REGION)

# Table objects
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

def safe_decimal(value: Any) -> Optional[Decimal]:
    try:
        if value is None:
            return None
        return Decimal(str(value))
    except Exception:
        return None


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
            "date": data.get("date", date.today().isoformat()),
            "note": data.get("note", ""),
            "type": data.get("type", "expense"),
            "createdAt": datetime.now(timezone.utc).isoformat(),
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
#----------for ses
def send_budget_alert_email(current_spent, budget):
    ses = boto3.client("ses", region_name="us-east-1")

    sender = "chintukadian588@gmail.com"       # ✅ verified email
    recipient = "chintukadian588@gmail.com"    # ✅ can be same or another verified
    subject = "⚠️ Budget Limit Exceeded!"
    body_text = (
        f"Hi there,\n\n"
        f"Your total spending this month has reached ₹{current_spent}, "
        f"which exceeds your set budget of ₹{budget}.\n\n"
        f"Please review your expenses in the Finance Tracker.\n\n"
        f"— Your Finance App"
    )

    try:
        response = ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body_text}}
            }
        )
        print("✅ Budget alert email sent! Message ID:", response["MessageId"])
    except ClientError as e:
        print("❌ SES Error:", e.response["Error"]["Message"])



# ---------- Budget endpoints (inline) ----------

# 🔹 Store current budget in memory (acts like a temporary DB)
current_budget = {
    "amount": 5000,  # default value
    "month": datetime.datetime.now().strftime("%Y-%m"),
    "alert_sent": False
}



@app.route("/budget", methods=["GET", "POST"])
def handle_budget():
    global current_budget  # use the global variable

    try:
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        transactions_table = dynamodb.Table("Transactions")

        if request.method == "POST":
            # 🟢 Log incoming data
            data = request.get_json()
            print("📩 Incoming POST data:", data)

            if not data or "amount" not in data:
                print("⚠️ No 'amount' found in request!")
                return jsonify({"error": "Missing 'amount' field"}), 400

            try:
                amount = float(data.get("amount"))
            except ValueError:
                print("❌ Invalid amount received:", data.get("amount"))
                return jsonify({"error": "Invalid budget amount"}), 400

            current_budget["amount"] = amount
            current_budget["alert_sent"] = False
            current_budget["month"] = datetime.datetime.now().strftime("%Y-%m")

            
            print(f"✅ Budget updated successfully: {current_budget}")
            return jsonify({
                "message": "Budget updated successfully",
                "budget": current_budget["amount"]
            }), 200

        # 🔹 Handle GET request
        print("📥 Fetching transactions from DynamoDB for budget check...")
        response = transactions_table.scan()
        items = response.get("Items", [])
        print(f"📦 Retrieved {len(items)} transactions")

        total_spent = sum(
            float(item.get("amount", 0))
            for item in items
            if item.get("type") == "expense"
        )

        print(f"💰 Total spent = {total_spent}, Budget = {current_budget['amount']}")
        if total_spent > current_budget["amount"]:
            if not current_budget.get("alert_sent"):  # only send once per budget cycle
                send_budget_alert_email(total_spent, current_budget["amount"])
                current_budget["alert_sent"] = True
        

        return jsonify({
            "month": current_budget["month"],
            "budget": current_budget["amount"],
            "spent": total_spent
        }), 200

    except Exception as e:
        print("❌ Error in /budget:", e)
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
        # print("📥 Fetching data from DynamoDB...")
        response = transactions_table.scan()
        items = response.get("Items", [])
        # print(f"✅ Retrieved {len(items)} transactions from DynamoDB.")

        # Handle pagination if there are more items
        while "LastEvaluatedKey" in response:
            response = transactions_table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
            # print(f"📄 Retrieved additional {len(response.get('Items', []))} items... Total now: {len(items)}")

        # 2️⃣ Convert to JSON
        # print("🔄 Converting items to JSON format...")
        json_data = json.dumps(items, indent=2, default=str)
        # print("✅ JSON conversion successful. Sample preview:")
        print(json_data[:300] + "..." if len(json_data) > 300 else json_data)

        # 3️⃣ Upload to S3
        bucket_name = "finance-app-exports"  # ⚠️ Replace with your actual S3 bucket name
        file_name = "transactions_export.json"
        # print(f"🚀 Uploading data to S3 bucket: {bucket_name} as {file_name} ...")

        s3.put_object(
            Bucket=bucket_name,
            Key=file_name,
            Body=json_data,
            ContentType="application/json"
        )

        # print("✅ Successfully uploaded transactions_export.json to S3.")
        return jsonify({"message": "Data exported successfully to S3", "total_items": len(items)}), 200

    except ClientError as e:
        # print("❌ AWS ClientError:", e)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        # print("❌ Unexpected Error:", e)
        return jsonify({"error": str(e)}), 500

# ----- Helpers -------------------------------------------------------------
def parse_amount_from_text(text: str, keywords=None) -> Optional[Decimal]:
    """
    Try to find an amount in text. Prefer lines containing keywords (total, amount).
    Return Decimal or None.
    """
    if not text:
        return None

    if keywords is None:
        keywords = ["total", "amount", "balance", "grand total", "net"]

    # Search lines with keyword + number
    for kw in keywords:
        pattern = rf"(?im).*{kw}.*?([0-9]+(?:[.,][0-9]{{1,2}})?)"
        m = re.search(pattern, text)
        if m:
            s = m.group(1).replace(",", ".")
            try:
                return Decimal(s)
            except Exception:
                continue

    # fallback: take the last monetary-looking number in whole text
    all_nums = re.findall(r"([0-9]+(?:[.,][0-9]{1,2})?)", text)
    if all_nums:
        s = all_nums[-1].replace(",", ".")
        try:
            return Decimal(s)
        except Exception:
            return None
    return None


def parse_date_from_text(text: str) -> Optional[str]:
    """
    Try to extract a date string (ISO) from text using common patterns, fallback to fuzzy parse.
    """
    if not text:
        return None

    # Common date patterns: dd/mm/yyyy, mm/dd/yyyy, yyyy-mm-dd
    candidates = re.findall(r"\b(?:\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})\b", text)
    for c in candidates:
        try:
            dt = parse_date(c, dayfirst=False, fuzzy=True)
            return dt.date().isoformat()
        except Exception:
            continue

    # Fuzzy parse entire text (last resort)
    try:
        dt = parse_date(text, fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return None


def ensure_table(table_name: str):
    """Ensure DynamoDB table exists; create if absent (requires proper IAM permissions)."""
    try:
        table = dynamodb.Table(table_name)
        table.load()  # will raise if table doesn't exist
        return table
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException":
            table = dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "transactionId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "transactionId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            table.meta.client.get_waiter("table_exists").wait(TableName=table_name)
            return dynamodb.Table(table_name)
        else:
            raise

# ----------upload file

def ensure_table(table_name):
    """Ensure DynamoDB table exists or create it."""
    try:
        table = dynamodb.Table(table_name)
        table.load()
    except:
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "transactionId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "transactionId", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName=table_name)
    return table

# ----------------------------------------
# 🧠 Utility Functions for OCR Extraction
# ----------------------------------------

def extract_total(text):
    """Extract the total amount using regex patterns."""
    pattern = r"(?i)(total|amount|balance)[^\d]*([\d,]+\.\d{2})"
    match = re.search(pattern, text)
    if match:
        return float(match.group(2).replace(",", ""))
    # fallback: look for standalone amounts like 123.45
    all_amounts = re.findall(r"\b\d{1,5}\.\d{2}\b", text)
    if all_amounts:
        return float(all_amounts[-1])
    return None

def extract_date(text):
    """Extract date in various formats."""
    pattern = r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    match = re.search(pattern, text)
    if match:
        try:
            raw = match.group(1)
            return str(datetime.strptime(raw, "%d/%m/%Y").date())
        except:
            try:
                return str(datetime.strptime(raw, "%m/%d/%Y").date())
            except:
                return raw  # fallback as string
    return None

def extract_vendor(text):
    """Guess vendor/store from first non-empty text line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else "Unknown Vendor"

# ----------------------------------------
# 📸 Upload Route
# ----------------------------------------

@app.route("/upload-receipt", methods=["POST"])
def upload_receipt():
    try:
        # Accept file (field name "file" or "receipt")
        file = request.files.get("file") or request.files.get("receipt")
        if not file:
            print("DEBUG: request.files keys:", list(request.files.keys()))
            print("DEBUG: request.form keys:", list(request.form.keys()))
            return jsonify({"ok": False, "error": "No file uploaded"}), 400

        # Category from form
        category = (request.form.get("category") or "").strip()
        user_id = request.form.get("userId") or "default-user"

        # Save file locally
        filename = file.filename or f"{uuid.uuid4().hex}.jpg"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        print(f"✅ File saved to {filepath}")

        # Preprocess image for OCR
        img = Image.open(filepath).convert("L")
        img = ImageOps.autocontrast(img)
        img = img.filter(ImageFilter.MedianFilter())

        # Extract text
        text = pytesseract.image_to_string(img, lang="eng")
        print("🧾 Extracted text sample:\n", text[:400])

        # Extract details
        total = extract_total(text)
        date = extract_date(text)
        vendor = extract_vendor(text)

        # Build transaction item
        transaction = {
            "transactionId": str(uuid.uuid4()),
            "userId": user_id,
            "vendor": vendor,
            "total": Decimal(str(total)) if total else None,
            "date": date,
            "category": category or "Uncategorized",
            "createdAt": datetime.utcnow().isoformat(),
            "rawText": text,
        }

        # Save to DynamoDB
        # table = ensure_table()
        # table.put_item(Item={k: v for k, v in transaction.items() if v is not None})

        # Prepare response (convert Decimal to str)
        transaction["total"] = str(transaction["total"]) if transaction["total"] else None

        return jsonify({"ok": True, "message": "Receipt processed successfully", "transaction": transaction}), 200

    except Exception as e:
        print("❌ Error in upload_receipt:", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.datetime.utcnow().isoformat() + "Z"}), 200


if __name__ == "__main__":
    host = getattr(config, "HOST", "0.0.0.0")
    port = int(getattr(config, "PORT", 5000))
    app.run(host=host, port=port, debug=True)
