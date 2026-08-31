import json
import os
import boto3

TABLE_NAME = os.environ.get("TABLE_NAME")
dynamodb = boto3.resource("dynamodb")

def handler(event, context):
    """Entry point for the URL shortener application."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"message": "url_shortener handler ready"}),
    }
