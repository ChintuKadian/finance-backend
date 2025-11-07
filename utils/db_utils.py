# utils/db_utils.py
import boto3
import os
import config

session_args = {}
if config.AWS_ACCESS_KEY_ID and config.AWS_SECRET_ACCESS_KEY:
    session_args["aws_access_key_id"] = config.AWS_ACCESS_KEY_ID
    session_args["aws_secret_access_key"] = config.AWS_SECRET_ACCESS_KEY
    if config.AWS_SESSION_TOKEN:
        session_args["aws_session_token"] = config.AWS_SESSION_TOKEN

region = os.environ.get("AWS_REGION", config.AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=region, **session_args)

def _convert_floats_to_decimal(item: dict):
    """
    DynamoDB expects decimals for numbers if using boto3 resource.
    Recursively replaces float with Decimal.
    """
    if isinstance(item, dict):
        return {k: _convert_floats_to_decimal(v) for k, v in item.items()}
    elif isinstance(item, list):
        return [_convert_floats_to_decimal(i) for i in item]
    elif isinstance(item, float):
        return Decimal(str(item))
    else:
        return item

def insert_transaction(table_name: str, transaction_item: dict):
    """
    Inserts transaction_item into DynamoDB table_name.
    transaction_item should be a plain dict with scalars/lists/dicts.
    """
    table = dynamodb.Table(table_name)
    item_to_put = _convert_floats_to_decimal(transaction_item)
    try:
        table.put_item(Item=item_to_put)
    except ClientError as e:
        raise RuntimeError(f"DynamoDB put_item failed: {e.response.get('Error', {}).get('Message')}")

# Additional helpers you might want
def get_transactions_for_user(table_name: str, user_id: str, limit: int = 100):
    table = dynamodb.Table(table_name)
    # Simple query assuming GSI or partition key is userId; adjust per your table schema
    # If userId is primary partition key:
    try:
        resp = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id),
            Limit=limit,
            ScanIndexForward=False
        )
        return resp.get('Items', [])
    except ClientError as e:
        raise RuntimeError(f"DynamoDB query failed: {e.response.get('Error', {}).get('Message')}")
