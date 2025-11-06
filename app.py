from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import boto3
import uuid
import os
import re
from datetime import datetime
from decimal import Decimal

app = Flask(__name__)
CORS(app)

#===for upload files setup
s3 = boto3.client('s3')
textract = boto3.client("textract")
BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "finance-tracker-store")


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

def upload_receipt():
    try:
        # 1️⃣ Validate file input
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files["file"]
        category = request.form.get("category", "Miscellaneous")
        user_id = request.form.get("userId", "default_user")

        # 2️⃣ Upload file to S3
        file_key = f"receipts/{user_id}/{uuid.uuid4()}_{file.filename}"
        s3.upload_fileobj(file, BUCKET_NAME, file_key)
        file_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{file_key}"

        # 3️⃣ Run Textract to extract text
        textract_response = textract.detect_document_text(
            Document={"S3Object": {"Bucket": BUCKET_NAME, "Name": file_key}}
        )

        full_text = " ".join(
            [block["Text"] for block in textract_response["Blocks"] if block["BlockType"] == "LINE"]
        ).lower()

        # 4️⃣ Extract amount (looking for "total" or "amount")
        amount = 0.0
        amount_match = re.search(r"(total|amount)[^\d]*(\d+[.,]?\d*)", full_text)
        if amount_match:
            amount = float(amount_match.group(2).replace(",", ""))

        # 5️⃣ Extract date
        date = None
        date_patterns = [
            r"\b\d{2}[/-]\d{2}[/-]\d{2,4}\b",  # 12/11/2024 or 12-11-2024
            r"\b\d{4}[/-]\d{2}[/-]\d{2}\b",    # 2024-11-12
        ]
        for pattern in date_patterns:
            match = re.search(pattern, full_text)
            if match:
                date = match.group(0)
                break

        if not date:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        # 6️⃣ Save in DynamoDB
        item = {
            "receiptId": str(uuid.uuid4()),
            "userId": user_id,
            "fileName": file.filename,
            "fileUrl": file_url,
            "category": category,
            "amount": Decimal(str(amount)),
            "date": date,
            "uploadDate": datetime.utcnow().isoformat(),
            "rawText": full_text[:500],  # store small snippet of text for reference
        }
        RECEIPT_TABLE.put_item(Item=item)

        # 7️⃣ Response
        return jsonify({
            "message": "Receipt uploaded and processed successfully",
            "fileUrl": file_url,
            "amount": amount,
            "date": date,
            "category": category,
        }), 200

    except Exception as e:
        print(f"❌ [upload-receipt] Error: {e}")
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
