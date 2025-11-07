# utils/s3_utils.py
import boto3
import mimetypes
import os
import config

# Use credentials from config only if provided, else rely on IAM role / environment
session_args = {}
if config.AWS_ACCESS_KEY_ID and config.AWS_SECRET_ACCESS_KEY:
    session_args["aws_access_key_id"] = config.AWS_ACCESS_KEY_ID
    session_args["aws_secret_access_key"] = config.AWS_SECRET_ACCESS_KEY
    if config.AWS_SESSION_TOKEN:
        session_args["aws_session_token"] = config.AWS_SESSION_TOKEN

# If you want to force region from config, pass region_name
region = os.environ.get("AWS_REGION", config.AWS_REGION)
s3_client = boto3.client("s3", region_name=region, **session_args)

def upload_fileobj_to_s3(fileobj, bucket_name: str, key: str, content_type: str = None):
    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type
    else:
        guessed_type, _ = mimetypes.guess_type(key)
        if guessed_type:
            extra_args["ContentType"] = guessed_type

    s3_client.upload_fileobj(Fileobj=fileobj, Bucket=bucket_name, Key=key, ExtraArgs=extra_args)

def make_s3_object_url(bucket_name: str, key: str) -> str:
    region = os.environ.get("AWS_REGION", config.AWS_REGION)
    return f"https://{bucket_name}.s3.{region}.amazonaws.com/{key}"
