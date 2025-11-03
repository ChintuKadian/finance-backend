import boto3
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table('Transactions')

@app.route('/add_transaction', methods=['POST'])
def add_transaction():
    data = request.get_json()
    table.put_item(Item=data)
    return jsonify({'message': 'Transaction added successfully!'})

@app.route('/get_transactions', methods=['GET'])
def get_transactions():
    response = table.scan()
    return jsonify(response['Items'])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
