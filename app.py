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
budget_table = dynamodb.Table('Budget')

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
    print("Received data:", data)
    new_tx = {
        "id": str(uuid.uuid4()),
        "amount": Decimal(str(data.get("amount", 0))),
        "category": data.get("category"),
        "date": data.get("date", datetime.now().strftime("%Y-%m-%d")),
        "note": data.get("note", ""),
        "type": data.get("type", "expense")
    }
    transactions_table.put_item(Item=new_tx)
    return jsonify(decimal_to_float(new_tx)), 201


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
@app.route("/budget", methods=["GET"])
def get_budget():
    # assuming only one budget record per month
    response = budget_table.scan()
    items = response.get('Items', [])
    if not items:
        return jsonify({"message": "No budget found"}), 404
    return jsonify(decimal_to_float(items[0]))


# ---- Set Budget ----
@app.route("/budget", methods=["POST"])
def set_budget():
    data = request.get_json()
    budget_item = {
        "month": data.get("month", datetime.now().strftime("%Y-%m")),
        "amount": Decimal(str(data.get("amount", 0))),
        "spent": Decimal(str(data.get("spent", 0)))
    }
    budget_table.put_item(Item=budget_item)
    return jsonify(decimal_to_float(budget_item))


# ---------- Run Server ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
