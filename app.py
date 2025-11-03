from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import uuid

app = Flask(__name__)
CORS(app)  # enable CORS for all routes

# Mock database
transactions = [
    {
        "id": "1",
        "amount": 5000,
        "category": "Salary",
        "date": "2025-10-01",
        "note": "Monthly salary",
        "type": "income"
    },
    {
        "id": "2",
        "amount": 1200,
        "category": "Rent",
        "date": "2025-10-05",
        "note": "Monthly rent payment",
        "type": "expense"
    },
    {
        "id": "3",
        "amount": 350,
        "category": "Groceries",
        "date": "2025-10-08",
        "note": "Weekly shopping",
        "type": "expense"
    }
]

budget = {
    "month": "2025-10",
    "amount": 4000,
    "spent": 1550
}

# -------------------------------
# ROUTES
# -------------------------------

@app.route("/")
def home():
    return jsonify({"message": "Finance backend is running!"})

# ---- Transactions ----
@app.route("/transactions", methods=["GET"])
def get_transactions():
    return jsonify(transactions)

@app.route("/transactions", methods=["POST"])
def add_transaction():
    data = request.get_json()
    new_tx = {
        "id": str(uuid.uuid4()),
        "amount": data.get("amount"),
        "category": data.get("category"),
        "date": data.get("date", datetime.now().strftime("%Y-%m-%d")),
        "note": data.get("note", ""),
        "type": data.get("type", "expense")
    }
    transactions.append(new_tx)
    return jsonify(new_tx), 201

# ---- Summary ----
@app.route("/summary", methods=["GET"])
def get_summary():
    total_income = sum(t["amount"] for t in transactions if t["type"] == "income")
    total_expense = sum(t["amount"] for t in transactions if t["type"] == "expense")
    balance = total_income - total_expense

    return jsonify({
        "totalIncome": total_income,
        "totalExpenses": total_expense,
        "balance": balance
    })

# ---- Budget ----
@app.route("/budget", methods=["GET"])
def get_budget():
    return jsonify(budget)

@app.route("/budget", methods=["POST"])
def set_budget():
    global budget
    budget = request.get_json()
    return jsonify(budget)

# -------------------------------
# RUN SERVER
# -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
