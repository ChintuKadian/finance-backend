# app.py - cleaned single-file backend for Finance Tracker (Flask + DynamoDB + optional S3/Textract + OCR)
import os
import io
import re
import uuid
import json
import datetime
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


# -------------------- Utilities --------------------
def decimal_to_native(obj):
    """Recursively convert DynamoDB Decimal -> float (so JSON is serializable)."""
    if isinstance(obj, list):
        return [decimal_to_native(i) for i in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        # convert to float (could convert to str if precision matters)
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


def ensure_table(table_name: str):
    """Ensure DynamoDB table exists; create if absent (requires IAM permissions)."""
    try:
        table = dynamodb.Table(table_name)
        table.load()
        return table
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            # Create a simple table with partition key 'id' if missing
            table = dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            table.meta.client.get_waiter("table_exists").wait(TableName=table_name)
            return dynamodb.Table(table_name)
        raise


# -------------------- Textract / OCR helpers --------------------
def extract_text_with_textract_s3(bucket: str, key: str) -> str:
    """Use Textract detect_document_text on an S3 object; return concatenated lines."""
    try:
        resp = textract.detect_document_text(Document={"S3Object": {"Bucket": bucket, "Name": key}})
        lines = [b.get("Text", "") for b in resp.get("Blocks", []) if b.get("BlockType") == "LINE"]
        return "\n".join(lines)
    except Exception as e:
        current_app.logger.warning("Textract failed: %s", e)
        return ""


def extract_text_from_image_path(path: str) -> str:
    """Preprocess an image and return OCR text using pytesseract."""
    try:
        img = Image.open(path).convert("L")
        img = ImageOps.autocontrast(img)
        img = img.filter(ImageFilter.MedianFilter())
        text = pytesseract.image_to_string(img, lang="eng")
        return text or ""
    except Exception as e:
        current_app.logger.exception("Tesseract OCR failed: %s", e)
        return ""


# -------------------- Parsing helpers --------------------
def parse_amount_from_text(text: str) -> Optional[Decimal]:
    """Extract a likely amount (Decimal) from text. Conservative heuristics."""
    if not text:
        return None

    # Prefer lines containing keywords
    keywords = ["total", "amount", "grand total", "net", "balance"]
    for kw in keywords:
        m = re.search(rf"(?mi){kw}[:\s]*([0-9]+(?:[.,][0-9]{{1,2}})?)", text)
        if m:
            try:
                s = m.group(1).replace(",", ".")
                return Decimal(s)
            except Exception:
                pass

    # fallback: last monetary-like number in text
    all_nums = re.findall(r"([0-9]+(?:[.,][0-9]{1,2})?)", text)
    if all_nums:
        try:
            s = all_nums[-1].replace(",", ".")
            return Decimal(s)
        except Exception:
            return None
    return None


def parse_date_from_text(text: str) -> Optional[str]:
    """Try to parse a date from text; return ISO date string (YYYY-MM-DD) or None."""
    if not text:
        return None

    # quick pattern catches common numeric dates
    candidates = re.findall(r"\b(?:\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})\b", text)
    for c in candidates:
        try:
            dt = parse_date(c, dayfirst=False, fuzzy=True)
            return dt.date().isoformat()
        except Exception:
            continue

    # fallback: try fuzzy parsing of whole text
    try:
        dt = parse_date(text, fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return None


def extract_vendor_from_text(text: str) -> str:
    """Guess vendor/store name as the first non-empty line (conservative)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[0] if lines else "Unknown Vendor"


# -------------------- Dynamo helpers --------------------
def insert_transaction_item(table_name: str, tx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert transaction into DynamoDB and return the stored item representation.
    Ensures 'id' and 'createdAt' exist, converts numeric to Decimal.
    """
    t = dict(tx)  # copy
    t.setdefault("id", str(uuid.uuid4()))
    t.setdefault("transactionId", t["id"])
    t.setdefault("userId", t.get("userId", "default_user"))
    t.setdefault("createdAt", datetime.datetime.utcnow().isoformat() + "Z")
    if "amount" in t and t["amount"] is not None:
        t["amount"] = Decimal(str(t["amount"]))
    # write
    table = dynamodb.Table(table_name)
    table.put_item(Item=t)
    # return JSON-friendly version
    return decimal_to_native(t)


# -------------------- Routes --------------------
@app.route("/", methods=["GET"])
def index():
    return jsonify({"message": "Finance backend is running", "region": AWS_REGION})


# GET all transactions (scan) — light-weight demo only
@app.route("/transactions", methods=["GET"])
def get_transactions():
    try:
        resp = transactions_table.scan()
        items = resp.get("Items", [])
        return jsonify(decimal_to_native(items))
    except Exception as e:
        current_app.logger.exception("Failed to scan transactions: %s", e)
        return jsonify({"error": str(e)}), 500


# POST add a transaction
@app.route("/transactions", methods=["POST"])
def add_transaction():
    try:
        data = request.get_json(force=True) or {}
        # amount: accept numeric or string
        amount = data.get("amount", 0)
        try:
            amount_val = float(amount)
        except Exception:
            amount_val = 0.0

        item = {
            "amount": amount_val,
            "category": data.get("category", "uncategorized"),
            "date": data.get("date", datetime.date.today().isoformat()),
            "note": data.get("note", ""),
            "type": data.get("type", "expense"),
            "userId": data.get("userId", "default_user"),
        }
        stored = insert_transaction_item(TRANSACTIONS_TABLE, item)
        return jsonify(stored), 201
    except Exception as e:
        current_app.logger.exception("Failed to add transaction: %s", e)
        return jsonify({"error": str(e)}), 500


# summary: simple totals
@app.route("/summary", methods=["GET"])
def get_summary():
    try:
        resp = transactions_table.scan()
        items = resp.get("Items", [])
        items_native = decimal_to_native(items)
        total_income = sum(float(t.get("amount", 0)) for t in items_native if t.get("type") == "income")
        total_expense = sum(float(t.get("amount", 0)) for t in items_native if t.get("type") == "expense")
        balance = total_income - total_expense
        return jsonify({"totalIncome": total_income, "totalExpenses": total_expense, "balance": balance})
    except Exception as e:
        current_app.logger.exception("Failed to compute summary: %s", e)
        return jsonify({"error": str(e)}), 500


# simple in-memory budget store (demo)
current_budget = {
    "amount": 5000,
    "month": datetime.datetime.now().strftime("%Y-%m"),
    "alert_sent": False,
}


def send_budget_alert_email(current_spent: float, budget_amount: float):
    """Sends an email using SES (ensure verified sender & permissions)."""
    try:
        ses = boto3.client("ses", region_name=AWS_REGION)
        sender = os.environ.get("BUDGET_ALERT_SENDER", None)
        recipient = os.environ.get("BUDGET_ALERT_RECIPIENT", sender)
        if not sender:
            current_app.logger.warning("SES sender not configured; skipping email.")
            return
        subject = "Budget limit exceeded"
        body = f"Spent {current_spent}, budget {budget_amount}"
        ses.send_email(Source=sender, Destination={"ToAddresses": [recipient]},
                       Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}})
        current_app.logger.info("Sent budget alert email")
    except Exception as e:
        current_app.logger.exception("Failed to send SES email: %s", e)


@app.route("/budget", methods=["GET", "POST"])
def handle_budget():
    global current_budget
    try:
        if request.method == "POST":
            data = request.get_json(force=True) or {}
            if "amount" not in data:
                return jsonify({"error": "Missing amount"}), 400
            try:
                amt = float(data["amount"])
            except Exception:
                return jsonify({"error": "Invalid amount"}), 400
            current_budget["amount"] = amt
            current_budget["month"] = datetime.datetime.now().strftime("%Y-%m")
            current_budget["alert_sent"] = False
            return jsonify({"message": "Budget updated", "budget": current_budget["amount"]}), 200

        # GET
        resp = transactions_table.scan()
        items = resp.get("Items", [])
        native = decimal_to_native(items)
        spent = sum(float(i.get("amount", 0)) for i in native if i.get("type") == "expense")
        if spent > current_budget["amount"] and not current_budget.get("alert_sent"):
            send_budget_alert_email(spent, current_budget["amount"])
            current_budget["alert_sent"] = True
        return jsonify({"month": current_budget["month"], "budget": current_budget["amount"], "spent": spent}), 200

    except Exception as e:
        current_app.logger.exception("Error in /budget: %s", e)
        return jsonify({"error": str(e)}), 500


# Export all transactions to an S3 object (JSON)
@app.route("/export-data", methods=["GET"])
def export_data_to_s3():
    try:
        resp = transactions_table.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = transactions_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        json_data = json.dumps(decimal_to_native(items), default=str, indent=2)
        bucket = os.environ.get("EXPORT_BUCKET", "finance-app-exports")
        key = f"transactions_export_{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"
        s3.put_object(Bucket=bucket, Key=key, Body=json_data, ContentType="application/json")
        return jsonify({"message": "exported", "items": len(items), "s3_key": key}), 200
    except Exception as e:
        current_app.logger.exception("Export failed: %s", e)
        return jsonify({"error": str(e)}), 500


# Upload and OCR receipt endpoint
@app.route("/upload-receipt", methods=["POST"])
def upload_receipt():
    try:
        # Accept file under "file" or "receipt"
        file = request.files.get("file") or request.files.get("receipt")
        if not file:
            current_app.logger.info("request.files keys: %s", list(request.files.keys()))
            return jsonify({"ok": False, "error": "No file uploaded"}), 400

        # Save locally
        filename = secure_filename(file.filename) or f"{uuid.uuid4().hex}.jpg"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        current_app.logger.info("Saved upload to %s", filepath)

        # Optionally: upload to S3 first if you want textract to use it
        # If S3_BUCKET is configured, put file there and call Textract; otherwise use pytesseract.
        ocr_text = ""
        if S3_BUCKET:
            s3_key = f"uploads/{filename}"
            try:
                # upload bytes
                if hasattr(file, "stream"):
                    body = file.stream.read()
                else:
                    # reopen saved file
                    with open(filepath, "rb") as fh:
                        body = fh.read()
                s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=body)
                ocr_text = extract_text_with_textract_s3(S3_BUCKET, s3_key)
            except Exception as e:
                current_app.logger.warning("S3/Textract path failed: %s", e)
                # fallback to local OCR
                ocr_text = extract_text_from_image_path(filepath)
        else:
            ocr_text = extract_text_from_image_path(filepath)

        # parse details
        total = parse_amount_from_text(ocr_text)
        date_iso = parse_date_from_text(ocr_text)
        vendor = extract_vendor_from_text(ocr_text)
        category = (request.form.get("category") or "").strip() or "Uncategorized"
        user_id = request.form.get("userId") or "default_user"

        transaction = {
            "transactionId": str(uuid.uuid4()),
            "userId": user_id,
            "vendor": vendor,
            "total": str(total) if total is not None else None,
            "date": date_iso,
            "category": category,
            "createdAt": datetime.datetime.utcnow().isoformat() + "Z",
            "rawText": ocr_text,
        }

        # Optionally save to DynamoDB — commented out if you don't want to persist here
        # table = ensure_table(TRANSACTIONS_TABLE)
        # table.put_item(Item={k: v for k, v in transaction.items() if v is not None})

        return jsonify({"ok": True, "transaction": transaction}), 200

    except Exception as e:
        current_app.logger.exception("upload_receipt failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# Simple healthcheck
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.datetime.utcnow().isoformat() + "Z"}), 200


# -------------------- Run --------------------
if __name__ == "__main__":
    host = getattr(config, "HOST", os.environ.get("HOST", "0.0.0.0"))
    port = int(getattr(config, "PORT", os.environ.get("PORT", 5000)))
    app.run(host=host, port=port, debug=True)
