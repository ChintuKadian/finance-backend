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

@app.route("/upload-and-add-transaction", methods=["POST"])
def upload_and_add_transaction():
    """
    Accepts form-data:
      - file: (required) receipt image/pdf
      - userId: (required) user identifier
      - category: (optional) category string
      - note: (optional) note string
    Returns created transaction JSON on success.
    """
    if "file" not in request.files:
        return jsonify({"error": "file is required"}), 400

    file = request.files["file"]
    user_id = request.form.get("userId") or request.form.get("user_id")
    if not user_id:
        return jsonify({"error": "userId is required"}), 400

    if not file or file.filename == "":
        return jsonify({"error": "no file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": f"file type not allowed. Allowed: {ALLOWED_EXTENSIONS}"}), 400

    filename = secure_filename(file.filename)
    # generate unique S3 key
    key = f"receipts/{user_id}/{uuid.uuid4().hex}_{filename}"

    try:
        # Upload to S3
        file.seek(0)
        upload_fileobj_to_s3(file.stream, S3_BUCKET, key, content_type=file.content_type)

        # Optionally wait a short time if needed; Textract works on S3 objects immediately typically.
        # Call Textract to extract text from the uploaded S3 object
        extracted_text = extract_text_from_s3(S3_BUCKET, key)

        # Parse amount and date (heuristics + regex)
        amount, date = parse_amount_and_date_from_text(extracted_text)

        # if amount missing, set to 0.0 so frontend can show it as unknown
        if amount is None:
            amount_value = 0.0
            inferred = False
        else:
            amount_value = float(amount)
            inferred = True

        # if date missing, use today's date
        if date is None:
            date_value = datetime.date.today().isoformat()
            date_inferred = False
        else:
            # date might be string; convert to ISO
            if isinstance(date, datetime.date):
                date_value = date.isoformat()
            else:
                date_value = str(date)
            date_inferred = True

        # Build the transaction item
        transaction_id = str(uuid.uuid4())
        category = request.form.get("category", "uncategorized")
        note = request.form.get("note", "")
        receipt_url = make_s3_object_url(S3_BUCKET, key)

        transaction_item = {
            "id": transaction_id,
            "userId": user_id,
            "amount": amount_value,
            "category": category,
            "date": date_value,
            "note": note,
            "type": "expense" if amount_value >= 0 else "income",  # naive; you might want logic based on sign or UI choice
            "receiptUrl": receipt_url,
            "createdAt": datetime.datetime.utcnow().isoformat() + "Z",
            "inferredFields": {
                "amountDetected": inferred,
                "dateDetected": date_inferred
            }
        }

        # Insert into DynamoDB Transactions table
        insert_transaction(TRANSACTIONS_TABLE, transaction_item)

        return jsonify({"transaction": transaction_item}), 201

    except Exception as e:
        app.logger.exception("Failed to upload and add transaction")
        return jsonify({"error": str(e)}), 500


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
