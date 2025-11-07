from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import boto3
import uuid
import os
import re
from datetime import datetime
from decimal import Decimal
import config 
from typing import Optional
from werkzeug.utils import secure_filename
from utils.s3_utils import upload_fileobj_to_s3, make_s3_object_url
from utils.textract_utils import extract_text_from_s3, parse_amount_and_date_from_text
from utils.db_utils import insert_transaction

app = Flask(__name__)
CORS(app)

#===for upload files setup
s3 = boto3.client("s3", region_name="us-east-1")
textract = boto3.client("textract", region_name="us-east-1")
BUCKET_NAME = "finance-tracker-store"
S3_BUCKET = os.environ.get("S3_BUCKET")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}

# ---------- DynamoDB Setup ----------
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')  # change region if needed
transactions_table = dynamodb.Table('Transactions')
budget_table = dynamodb.Table('UserBudgets')

# ---------- Helper function ----------
def decimal_to_float(obj):
    """Convert DynamoDB Decimals to normal float/int for JSON serialization"""
    if isinstance(obj, list):
        return [decimal_to_float(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: decimal_to_float(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        return float(obj)
    return obj

# ---------- Routes ----------

@app.route("/")
def home():
    return jsonify({"message": "Finance backend connected to DynamoDB!"})


# ---- Get all transactions ----
@app.route("/transactions", methods=["GET"])
def get_transactions():
    response = transactions_table.scan()
    items = response.get('Items', [])
    return jsonify(decimal_to_float(items))


# ---- Add a transaction ----
@app.route("/transactions", methods=["POST"])
def add_transaction():
    data = request.get_json()
    new_tx = {
        "userId": "default_user",  # ✅ Add this line (required by DynamoDB)
        "transactionId": str(uuid.uuid4()),
        "amount": data.get("amount"),
        "category": data.get("category"),
        "date": data.get("date", datetime.now().strftime("%Y-%m-%d")),
        "note": data.get("note", ""),
        "type": data.get("type", "expense")
    }

    print("Adding transaction:", new_tx)  # Optional for debugging
    transactions_table.put_item(Item=new_tx)
    return jsonify(new_tx), 201


# ---- Summary ----
@app.route("/summary", methods=["GET"])
def get_summary():
    response = transactions_table.scan()
    items = response.get('Items', [])

    total_income = sum(float(t["amount"]) for t in items if t["type"] == "income")
    total_expense = sum(float(t["amount"]) for t in items if t["type"] == "expense")
    balance = total_income - total_expense

    return jsonify({
        "totalIncome": total_income,
        "totalExpenses": total_expense,
        "balance": balance
    })


# ---- Budget Routes (Using UserBudgetsNew table) ----

@app.route("/budget", methods=["GET"])
def get_budget():
    try:
        print("🔍 [GET /budget] Fetching budget from table: UserBudgetsNew")
        response = budget_table.scan()
        items = response.get('Items', [])
        print(f"✅ [GET /budget] Items fetched: {items}")

        if not items:
            return jsonify({"message": "No budget found"}), 404

        # 🔄 Convert DynamoDB Decimals and rename fields for frontend compatibility
        formatted_items = []
        for item in items:
            formatted_items.append({
                "month": item.get("month", ""),
                "amount": float(item.get("budgetLimit", 0)),
                "spent": float(item.get("spent", 0)),
                "category": item.get("category", ""),
                "userId": item.get("userId", "")
            })

        return jsonify(formatted_items)

    except Exception as e:
        print(f"❌ [GET /budget] Error: {e}")
        return jsonify({"error": str(e)}), 500

# ---- add budgets 
@app.route("/budget", methods=["POST"])
def add_budget():
    try:
        data = request.get_json()
        print("📝 [POST /budget] Received data:", data)

        user_id = data.get("userId")
        month = data.get("month")
        budget_limit = data.get("budgetLimit")
        category = data.get("category")
        spent = data.get("spent", 0)

        if not all([user_id, month, budget_limit, category]):
            return jsonify({"error": "Missing required fields"}), 400

        budget_item = {
            "userId": user_id,
            "month": month,
            "category": category,
            "budgetLimit": Decimal(str(budget_limit)),
            "spent": Decimal(str(spent))
        }

        # Save to DynamoDB
        budget_table.put_item(Item=budget_item)
        print("✅ [POST /budget] Saved successfully:", budget_item)
        return jsonify({"message": "Budget added successfully", "item": budget_item}), 200

    except Exception as e:
        print(f"❌ [POST /budget] Error: {e}")
        return jsonify({"error": str(e)}), 500

#------------upload files
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _normalize_amount(form_amount: Optional[str], parsed_amount: Optional[float]) -> (Optional[float], bool):
    """
    Returns (amount_value_or_None, was_inferred_bool)
    - If form_amount is provided and valid, prefer it (inferred=False)
    - Else if parsed_amount is provided, use it (inferred=True)
    - Else return (None, False)
    """
    if form_amount:
        try:
            # Allow commas in input like "1,234.56"
            cleaned = form_amount.replace(",", "").strip()
            val = float(cleaned)
            return val, False
        except Exception:
            # invalid user-provided amount — fall back to parsed_amount
            pass

    if parsed_amount is not None:
        try:
            return float(parsed_amount), True
        except Exception:
            return None, False

    return None, False


def _normalize_date(form_date: Optional[str], parsed_date) -> (Optional[str], bool):
    """
    Returns (date_iso_or_None, was_inferred_bool)
    - If form_date is provided and valid ISO-like, prefer it (inferred=False)
    - Else if parsed_date is a date object, convert to ISO and return (inferred=True)
    - Else None
    """
    if form_date:
        fd = form_date.strip()
        # Try common formats: ISO first, then some slashed formats
        try:
            # try direct ISO (YYYY-MM-DD)
            dt = datetime.date.fromisoformat(fd)
            return dt.isoformat(), False
        except Exception:
            # try dd/mm/yyyy or dd-mm-yyyy
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y"):
                try:
                    dt = datetime.datetime.strptime(fd, fmt).date()
                    return dt.isoformat(), False
                except Exception:
                    continue
            # If not parseable, ignore and fallback to parsed_date
    if parsed_date:
        # parsed_date may be a datetime.date already (from textract_utils)
        if isinstance(parsed_date, (datetime.date, datetime.datetime)):
            return parsed_date.isoformat(), True
        else:
            # if parsed_date is string, try to normalize
            try:
                # try parse as ISO first
                dt = datetime.date.fromisoformat(str(parsed_date))
                return dt.isoformat(), True
            except Exception:
                pass
    return None, False


@app.route("/upload-and-add-transaction", methods=["POST"])
def upload_and_add_transaction():
    """
    Accepts form-data:
      - file: (required) receipt image or pdf
      - userId: (required) user identifier
      - category: (optional) category string
      - note: (optional) note string
      - amount: (optional) numeric override (string allowed)
      - date: (optional) date override (string allowed)
    Behavior:
      - Upload file to S3
      - Call Textract (detect_document_text) to extract text
      - Parse amount & date from Textract output (fallback heuristics)
      - If frontend provided amount/date and they are valid, prefer them
      - Insert transaction into DynamoDB and return the created item JSON
    """
    # Basic validations
    if "file" not in request.files:
        return jsonify({"error": "file is required"}), 400

    file = request.files["file"]
    if not file or file.filename == "":
        return jsonify({"error": "no file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": f"file type not allowed. Allowed: {ALLOWED_EXTENSIONS}"}), 400

    user_id = request.form.get("userId") or request.form.get("user_id")
    if not user_id:
        return jsonify({"error": "userId is required"}), 400

    filename = secure_filename(file.filename)
    key = f"receipts/{user_id}/{uuid.uuid4().hex}_{filename}"

    # read optional overrides from client
    form_amount = request.form.get("amount")  # may be None
    form_date = request.form.get("date")      # may be None
    category = request.form.get("category", "uncategorized")
    note = request.form.get("note", "")

    try:
        # Upload to S3
        file.seek(0)
        upload_fileobj_to_s3(file.stream, S3_BUCKET, key, content_type=file.content_type)

        # Extract text from S3 via Textract
        extracted_text = extract_text_from_s3(S3_BUCKET, key)

        # Parse amount+date from extracted text (heuristic)
        parsed_amount, parsed_date = parse_amount_and_date_from_text(extracted_text)

        # Prefer client overrides if valid; otherwise use parsed values
        amount_value, amount_inferred = _normalize_amount(form_amount, parsed_amount)
        date_value, date_inferred = _normalize_date(form_date, parsed_date)

        # Final fallback defaults
        if amount_value is None:
            # If no amount from either source, set to 0.0 but mark as not detected/inferred = False
            amount_value = 0.0
            amount_inferred = False

        if date_value is None:
            # default to today's date
            date_value = datetime.date.today().isoformat()
            date_inferred = False
        else:
            date_inferred = date_inferred

        # Build transaction item
        transaction_id = str(uuid.uuid4())
        receipt_url = make_s3_object_url(S3_BUCKET, key)

        # naive type assignment: if amount > 0 treat as expense, else income (adjust as needed)
        tx_type = "expense" if float(amount_value) >= 0 else "income"

        transaction_item = {
            "id": transaction_id,
            "userId": user_id,
            "amount": float(amount_value),
            "category": category,
            "date": date_value,
            "note": note,
            "type": tx_type,
            "receiptUrl": receipt_url,
            "createdAt": datetime.datetime.utcnow().isoformat() + "Z",
            "inferredFields": {
                "amountDetected": bool(parsed_amount is not None),
                "dateDetected": bool(parsed_date is not None),
                "amountOverriddenByUser": bool(form_amount is not None),
                "dateOverriddenByUser": bool(form_date is not None),
                "finalAmountInferred": bool(amount_inferred),
                "finalDateInferred": bool(date_inferred),
            },
            # optionally save the raw extracted_text for debugging (comment out in prod)
            # "rawExtractedText": extracted_text
        }

        # Insert into DynamoDB
        insert_transaction(TRANSACTIONS_TABLE, transaction_item)

        return jsonify({"transaction": transaction_item}), 201

    except Exception as exc:
        app.logger.exception("Failed to upload and add transaction")
        return jsonify({"error": str(exc)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.datetime.utcnow().isoformat() + "Z"}), 200


if __name__ == "__main__":
    host = getattr(config, "HOST", "0.0.0.0")
    port = int(getattr(config, "PORT", 5000))
    app.run(host=host, port=port, debug=True)

@app.route('/files', methods=['GET'])
def list_files():
    try:
        response = s3.list_objects_v2(Bucket=BUCKET_NAME)
        files = [obj['Key'] for obj in response.get('Contents', [])]
        return jsonify(files)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    

# ---------- Run Server ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
