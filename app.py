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


# ---- Get Budget ----
# ---- Get Budget ----
@app.route("/budget", methods=["GET"])
def get_budget():
    print("🔍 [GET /budget] Fetching budget from table:", budget_table.name)
    try:
        # Scan all budget entries (for now, assuming one budget per user)
        response = budget_table.scan()
        items = response.get('Items', [])
        print("✅ [GET /budget] Items fetched:", items)

        if not items:
            print("⚠️ [GET /budget] No budget data found in DynamoDB.")
            return jsonify({"message": "No budget found"}), 404

        # Take first item and format it for frontend
        item = items[0]
        budget_data = {
            "month": item.get("month", datetime.now().strftime("%Y-%m")),
            "amount": float(item.get("budgetLimit", item.get("amount", 0))),
            "spent": float(item.get("spent", 0)),
            "category": item.get("category", "General")
        }

        print("✅ [GET /budget] Formatted data for frontend:", budget_data)
        return jsonify(budget_data)

    except Exception as e:
        print("❌ [GET /budget] Error while fetching:", str(e))
        return jsonify({"error": str(e)}), 500


# ---- Set Budget ----
@app.route("/budget", methods=["POST"])
def set_budget():
    try:
        data = request.get_json()
        print("📩 [POST /budget] Data received from frontend:", data)

        # Build item to store in DynamoDB
        budget_item = {
            "userId": "default_user",  # adjust this if you have user auth later
            "month": data.get("month", datetime.now().strftime("%Y-%m")),
            "category": data.get("category", "General"),
            "budgetLimit": Decimal(str(data.get("amount", 0))),
            "spent": Decimal(str(data.get("spent", 0)))
        }

        print("🪣 [POST /budget] Saving item to DynamoDB:", budget_item)
        budget_table.put_item(Item=budget_item)

        # Prepare response for frontend
        response_data = {
            "month": budget_item["month"],
            "amount": float(budget_item["budgetLimit"]),
            "spent": float(budget_item["spent"]),
            "category": budget_item["category"]
        }

        print("✅ [POST /budget] Saved successfully:", response_data)
        return jsonify(response_data)

    except Exception as e:
        print("❌ [POST /budget] Error while saving:", str(e))
        return jsonify({"error": str(e)}), 500


# ---------- Run Server ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
