from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import boto3
import uuid
from decimal import Decimal

app = Flask(__name__)
CORS(app)

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
        return jsonify(decimal_to_float(items))
    except Exception as e:
        print(f"❌ [GET /budget] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/budget", methods=["POST"])
def set_budget():
    try:
        data = request.get_json()
        print(f"📩 [POST /budget] Data received: {data}")

        user_id = data.get("userId", "default_user")
        month = data.get("month", datetime.now().strftime("%Y-%m"))
        budget_limit = Decimal(str(data.get("budgetLimit", 0)))
        spent = Decimal(str(data.get("spent", 0)))
        category = data.get("category", "General")

        budget_item = {
            "userId": user_id,
            "month": month,
            "budgetLimit": budget_limit,
            "spent": spent,
            "category": category
        }

        print(f"🧾 [POST /budget] Writing item to DynamoDB: {budget_item}")
        budget_table.put_item(Item=budget_item)

        print("✅ [POST /budget] Budget saved successfully")
        return jsonify(decimal_to_float(budget_item)), 201
    except Exception as e:
        print(f"❌ [POST /budget] Error: {e}")
        return jsonify({"error": str(e)}), 500

# ---------- Run Server ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
