# app.py - corrected single-file backend for Finance Tracker (Flask + DynamoDB + optional S3/Textract + OCR)
import os
import io
import re
import uuid
import json
from decimal import Decimal
from typing import Optional, Dict, Any

from datetime import datetime, date, timezone
from dateutil.parser import parse as parse_date
from PIL import Image, ImageOps, ImageFilter
import pytesseract

from flask import Flask, request, jsonify, current_app
from flask_cors import CORS
from werkzeug.utils import secure_filename

import boto3
from botocore.exceptions import ClientError

import config  # optional local config

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
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)
textract = boto3.client("textract", region_name=AWS_REGION)

# Table objects
transactions_table = dynamodb.Table(TRANSACTIONS_TABLE)
budgets_table = dynamodb.Table(BUDGETS_TABLE)


# -------------------- Utilities --------------------
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


# -------------------- S3 helpers --------------------
def upload_fileobj_to_s3(file_obj, bucket: str, key: str, content_type: str = None):
    """Upload a file-like object to S3. `file_obj` may be werkzeug FileStorage or stream."""
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
    return f"https://{bucket}.s3.amazonaws.com/{key}"


# -------------------- Textract / OCR helpers --------------------
def extract_text_from_s3(bucket: str, key: str) -> str:
    try:
        resp = textract.detect_document_text(Document={"S3Object": {"Bucket": bucket, "Name": key}})
        lines = [b.get("Text", "") for b in resp.get("Blocks", []) if b.get("BlockType") == "LINE"]
        return "\n".join(lines)
    except Exception as e:
        current_app.logger.warning("Textract failed: %s", e)
        return ""


def extract_text_from_image_path(path: str) -> str:
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
    if not text:
        return None
    keywords = ["total", "amount", "grand total", "net", "balance"]
    for kw in keywords:
        m = re.search(rf"(?mi){kw}[:\s]*([0-9]+(?:[.,][0-9]{{1,2}})?)", text)
        if m:
            try:
                s = m.group(1).replace(",", ".")
                return Decimal(s)
            except Exception:
                pass
    all_nums = re.findall(r"([0-9]+(?:[.,][0-9]{1,2})?)", text)
    if all_nums:
        try:
            s = all_nums[-1].replace(",", ".")
            return Decimal(s)
        except Exception:
            return None
    return None


def parse_date_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    candidates = re.findall(r"\b(?:\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})\b", text)
    for c in candidates:
        try:
            dt = parse_date(c, dayfirst=False, fuzzy=True)
            return dt.date().isoformat()
        except Exception:
            continue
    try:
        dt = parse_date(text, fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return None


def extract_vendor_from_text(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[0] if lines else "Unknown Vendor"


# -------------------- Dynamo helpers --------------------
def ensure_table(table_name: str):
    """Ensure DynamoDB table exists; create if absent (requires IAM permissions)."""
    try:
        table = dynamodb.Table(table_name)
        table.load()
        return table
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            table = dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            table.meta.client.get_waiter("table_exists").wait(TableName=table_name)
            return dynamodb.Table(table_name)
        raise


def insert_transaction(table_name: str, tx_item: dict):
    """Insert a transaction into DynamoDB. Converts floats to Decimal."""
    table = dynamodb.Table(table_name)
    item = dict(tx_item)
    item.setdefault("id", str(uuid.uuid4()))
    item.setdefault("transactionId", item["id"])
    item.setdefault("userId", item.get("userId", "default_user"))
    item.setdefault("createdAt", datetime.now(timezone.utc).isoformat())
    if "amount" in item and item["amount"] is not None:
        item["amount"] = Decimal(str(item["amount"]))
    try:
        table.put_item(Item=item)
    except ClientError as e:
        app.logger.exception("DynamoDB put_item failed: %s", e)
        raise


# -------------------- Routes --------------------
@app.route("/", methods=["GET"])
def home():
    return jsonify({"message": "Finance backend connected to DynamoDB!"})


@app.route("/transactions", methods=["GET"])
def get_transactions():
    try:
        resp = transactions_table.scan()
        items = resp.get("Items", [])
        return jsonify(decimal_to_float(items))
    except Exception as e:
        app.logger.exception("Failed to scan transactions: %s", e)
        return jsonify({"error": str(e)}), 500


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
        app.logger.exception("Failed to add transaction: %s", e)
        return jsonify({"error": str(e)}), 500


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
        app.logger.exception("Failed to compute summary: %s", e)
        return jsonify({"error": str(e)}), 500


# helper
def _to_float_safe(v):
    try:
        return float(v)
    except Exception:
        return 0.0


# -------------------- Budget --------------------
current_budget = {
    "amount": 5000,
    "month": datetime.now(timezone.utc).strftime("%Y-%m"),
    "alert_sent": False,
}


def send_budget_alert_email(current_spent, budget):
    try:
        ses = boto3.client("ses", region_name=AWS_REGION)
        sender = os.environ.get("BUDGET_ALERT_SENDER", None)
        recipient = os.environ.get("BUDGET_ALERT_RECIPIENT", sender)
        if not sender:
            current_app.logger.warning("SES sender not configured; skipping email.")
            return
        subject = "Budget limit exceeded"
        body = f"Spent {current_spent}, budget {budget}"
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
            data = request.get_json() or {}
            if "amount" not in data:
                return jsonify({"error": "Missing amount"}), 400
            try:
                amt = float(data["amount"])
            except Exception:
                return jsonify({"error": "Invalid amount"}), 400
            current_budget["amount"] = amt
            current_budget["month"] = datetime.now(timezone.utc).strftime("%Y-%m")
            current_budget["alert_sent"] = False
            return jsonify({"message": "Budget updated", "budget": current_budget["amount"]}), 200

        resp = transactions_table.scan()
        items = resp.get("Items", [])
        spent = sum(float(i.get("amount", 0)) for i in items if i.get("type") == "expense")
        if spent > current_budget["amount"] and not current_budget.get("alert_sent"):
            send_budget_alert_email(spent, current_budget["amount"])
            current_budget["alert_sent"] = True
        return jsonify({"month": current_budget["month"], "budget": current_budget["amount"], "spent": spent}), 200

    except Exception as e:
        current_app.logger.exception("Error in /budget: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/export-data", methods=["GET"])
def export_data_to_s3():
    try:
        resp = transactions_table.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = transactions_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        json_data = json.dumps(decimal_to_float(items), default=str, indent=2)
        bucket = os.environ.get("EXPORT_BUCKET", "finance-app-exports")
        key = f"transactions_export_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        s3.put_object(Bucket=bucket, Key=key, Body=json_data, ContentType="application/json")
        return jsonify({"message": "exported", "items": len(items), "s3_key": key}), 200
    except Exception as e:
        current_app.logger.exception("Export failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/upload-receipt", methods=["POST"])
def upload_receipt():
    try:
        file = request.files.get("file") or request.files.get("receipt")
        if not file:
            current_app.logger.info("request.files keys: %s", list(request.files.keys()))
            current_app.logger.info("request.form keys: %s", list(request.form.keys()))
            return jsonify({"ok": False, "error": "No file uploaded"}), 400

        category = (request.form.get("category") or "").strip()
        user_id = request.form.get("userId") or "default-user"

        filename = secure_filename(file.filename) or f"{uuid.uuid4().hex}.jpg"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        current_app.logger.info("Saved upload to %s", filepath)

        ocr_text = ""
        if S3_BUCKET:
            s3_key = f"uploads/{filename}"
            try:
                if hasattr(file, "stream"):
                    body = file.stream.read()
                else:
                    with open(filepath, "rb") as fh:
                        body = fh.read()
                s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=body)
                ocr_text = extract_text_from_s3(S3_BUCKET, s3_key)
            except Exception as e:
                current_app.logger.warning("S3/Textract path failed: %s", e)
                ocr_text = extract_text_from_image_path(filepath)
        else:
            ocr_text = extract_text_from_image_path(filepath)

        total = parse_amount_from_text(ocr_text)
        date_iso = parse_date_from_text(ocr_text)
        vendor = extract_vendor_from_text(ocr_text)
        category = category or "Uncategorized"

        transaction = {
            "transactionId": str(uuid.uuid4()),
            "userId": user_id,
            "vendor": vendor,
            "total": str(total) if total is not None else None,
            "date": date_iso,
            "category": category,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "rawText": ocr_text,
        }

        return jsonify({"ok": True, "transaction": transaction}), 200

    except Exception as e:
        current_app.logger.exception("upload_receipt failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}), 200


if __name__ == "__main__":
    host = getattr(config, "HOST", os.environ.get("HOST", "0.0.0.0"))
    port = int(getattr(config, "PORT", os.environ.get("PORT", 5000)))
    app.run(host=host, port=port, debug=True)
