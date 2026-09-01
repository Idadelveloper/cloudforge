############################################################
# DynamoDB - order persistence (plan: DynamoDB "orders")
############################################################

resource "aws_dynamodb_table" "orders" {
  # checkov:skip=CKV_AWS_119: Encrypted with the AWS managed DynamoDB key; a customer managed KMS key is not part of the approved resource plan and KMS is out of scope for this deployment.
  name         = var.orders_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  attribute {
    name = "customer_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = var.orders_customer_index_name
    hash_key        = "customer_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name      = var.orders_table_name
    component = "order-persistence"
  }
}

############################################################
# SQS - fulfilment queue + dead letter queue
############################################################

resource "aws_sqs_queue" "order_fulfillment_dlq" {
  # checkov:skip=CKV_AWS_27: SQS managed server side encryption is enabled; no customer managed KMS key exists in the approved resource plan.
  name                       = var.fulfillment_dlq_name
  message_retention_seconds  = var.dlq_message_retention_seconds
  visibility_timeout_seconds = var.queue_visibility_timeout_seconds
  sqs_managed_sse_enabled    = true

  tags = {
    Name      = var.fulfillment_dlq_name
    component = "order-fulfillment"
  }
}

resource "aws_sqs_queue" "order_fulfillment" {
  # checkov:skip=CKV_AWS_27: SQS managed server side encryption is enabled; no customer managed KMS key exists in the approved resource plan.
  name                       = var.fulfillment_queue_name
  message_retention_seconds  = var.queue_message_retention_seconds
  visibility_timeout_seconds = var.queue_visibility_timeout_seconds
  receive_wait_time_seconds  = 10
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.order_fulfillment_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name      = var.fulfillment_queue_name
    component = "order-fulfillment"
  }
}

############################################################
# SNS - order status change notifications
############################################################

resource "aws_sns_topic" "order_status_changed" {
  name              = var.status_topic_name
  display_name      = "Order status changed"
  kms_master_key_id = "alias/aws/sns"

  tags = {
    Name      = var.status_topic_name
    component = "order-notifications"
  }
}

############################################################
# CloudWatch - log group for the fulfilment worker
############################################################

resource "aws_cloudwatch_log_group" "order_fulfillment_worker" {
  # checkov:skip=CKV_AWS_158: Encrypted with the default CloudWatch Logs encryption; a customer managed KMS key is not part of the approved resource plan.
  name              = "/aws/lambda/${var.worker_function_name}"
  retention_in_days = var.log_retention_days

  tags = {
    Name      = "/aws/lambda/${var.worker_function_name}"
    component = "order-fulfillment"
  }
}

############################################################
# IAM - fulfilment worker execution role (least privilege)
############################################################

data "aws_iam_policy_document" "worker_assume_role" {
  statement {
    sid     = "LambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "order_fulfillment_worker" {
  name               = var.worker_role_name
  description        = "Execution role for the order fulfilment worker Lambda"
  assume_role_policy = data.aws_iam_policy_document.worker_assume_role.json

  tags = {
    Name      = var.worker_role_name
    component = "order-fulfillment"
  }
}

data "aws_iam_policy_document" "worker_permissions" {
  statement {
    sid    = "ConsumeFulfillmentQueue"
    effect = "Allow"

    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]

    resources = [aws_sqs_queue.order_fulfillment.arn]
  }

  statement {
    sid       = "SendToDeadLetterQueue"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.order_fulfillment_dlq.arn]
  }

  statement {
    sid    = "UpdateOrderRecords"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
    ]

    resources = [aws_dynamodb_table.orders.arn]
  }

  statement {
    sid       = "PublishStatusChange"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.order_status_changed.arn]
  }

  statement {
    sid    = "WriteWorkerLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.order_fulfillment_worker.arn}:*"]
  }
}

resource "aws_iam_role_policy" "order_fulfillment_worker" {
  name   = "${var.worker_role_name}-policy"
  role   = aws_iam_role.order_fulfillment_worker.id
  policy = data.aws_iam_policy_document.worker_permissions.json
}

############################################################
# IAM - least privilege policy for the FastAPI order service
############################################################

data "aws_iam_policy_document" "order_api_service" {
  statement {
    sid    = "OrdersTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.orders.arn,
      "${aws_dynamodb_table.orders.arn}/index/${var.orders_customer_index_name}",
    ]
  }

  statement {
    sid    = "EnqueueFulfillmentMessages"
    effect = "Allow"

    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
    ]

    resources = [aws_sqs_queue.order_fulfillment.arn]
  }

  statement {
    sid    = "PublishOrderStatusEvents"
    effect = "Allow"

    actions = [
      "sns:Publish",
      "sns:GetTopicAttributes",
    ]

    resources = [aws_sns_topic.order_status_changed.arn]
  }
}

resource "aws_iam_policy" "order_api_service" {
  name        = var.api_service_policy_name
  description = "Least privilege access for the FastAPI order processing service"
  policy      = data.aws_iam_policy_document.order_api_service.json

  tags = {
    Name      = var.api_service_policy_name
    component = "order-api"
  }
}

############################################################
# Lambda - order fulfilment worker (inline packaged source)
############################################################

resource "local_file" "order_fulfillment_worker_source" {
  filename        = "${path.module}/build/index.py"
  file_permission = "0644"

  content = <<-PYCODE
    """SQS triggered worker that advances order status and notifies SNS."""
    import json
    import logging
    from datetime import datetime, timezone

    import boto3
    from botocore.exceptions import ClientError

    LOGGER = logging.getLogger()
    LOGGER.setLevel(logging.INFO)

    TABLE_NAME = "${aws_dynamodb_table.orders.name}"
    TOPIC_ARN = "${aws_sns_topic.order_status_changed.arn}"
    TARGET_STATUS = "FULFILLED"

    _dynamodb = boto3.resource("dynamodb")
    _sns = boto3.client("sns")
    _table = _dynamodb.Table(TABLE_NAME)


    def _now():
        return datetime.now(timezone.utc).isoformat()


    def _publish_status_change(order_id, customer_id, previous_status, new_status, reason):
        event = {
            "order_id": order_id,
            "customer_id": customer_id,
            "previous_status": previous_status,
            "new_status": new_status,
            "reason": reason,
            "changed_at": _now(),
        }
        _sns.publish(
            TopicArn=TOPIC_ARN,
            Subject="order-status-changed",
            Message=json.dumps(event),
            MessageAttributes={
                "event_type": {"DataType": "String", "StringValue": "order.status_changed"},
                "new_status": {"DataType": "String", "StringValue": new_status},
            },
        )
        return event


    def _process_record(record):
        try:
            message = json.loads(record.get("body") or "{}")
        except ValueError:
            LOGGER.warning("discarding non JSON message: %s", record.get("body"))
            return

        order_id = message.get("order_id")
        if not order_id:
            LOGGER.warning("discarding message without order_id: %s", message)
            return

        try:
            result = _table.update_item(
                Key={"order_id": order_id},
                UpdateExpression="SET #status = :new_status, updated_at = :ts",
                ConditionExpression="attribute_exists(order_id)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":new_status": TARGET_STATUS, ":ts": _now()},
                ReturnValues="ALL_OLD",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                LOGGER.warning("order %s no longer exists, dropping message", order_id)
                return
            raise

        previous = result.get("Attributes") or {}
        previous_status = previous.get("status", message.get("status", "UNKNOWN"))
        customer_id = previous.get("customer_id", message.get("customer_id", ""))

        _publish_status_change(
            order_id,
            customer_id,
            previous_status,
            TARGET_STATUS,
            "fulfilled by order-fulfillment-worker",
        )
        LOGGER.info("order %s advanced from %s to %s", order_id, previous_status, TARGET_STATUS)


    def handler(event, context):
        failures = []
        for record in event.get("Records", []):
            try:
                _process_record(record)
            except Exception:  # noqa: BLE001 - report the record so SQS can retry it
                LOGGER.exception("failed to process record %s", record.get("messageId"))
                failures.append({"itemIdentifier": record.get("messageId")})
        return {"batchItemFailures": failures}
  PYCODE
}

data "archive_file" "order_fulfillment_worker" {
  type        = "zip"
  source_file = local_file.order_fulfillment_worker_source.filename
  output_path = "${path.module}/build/order-fulfillment-worker.zip"

  depends_on = [local_file.order_fulfillment_worker_source]
}

resource "aws_lambda_function" "order_fulfillment_worker" {
  # checkov:skip=CKV_AWS_117: The worker only talks to regional AWS APIs (DynamoDB, SQS, SNS); no VPC resources are part of the approved resource plan.
  # checkov:skip=CKV_AWS_272: Code signing requires an AWS Signer profile, which is not part of the approved resource plan and is unsupported by the target environment.
  function_name = var.worker_function_name
  description   = "Consumes order fulfilment messages from SQS, updates DynamoDB and notifies SNS"
  role          = aws_iam_role.order_fulfillment_worker.arn
  handler       = "index.handler"
  runtime       = var.worker_runtime

  filename         = data.archive_file.order_fulfillment_worker.output_path
  source_code_hash = data.archive_file.order_fulfillment_worker.output_base64sha256

  timeout                        = var.worker_timeout_seconds
  memory_size                    = var.worker_memory_size
  reserved_concurrent_executions = var.worker_reserved_concurrency

  tracing_config {
    mode = "Active"
  }

  dead_letter_config {
    target_arn = aws_sqs_queue.order_fulfillment_dlq.arn
  }

  depends_on = [
    aws_iam_role_policy.order_fulfillment_worker,
    aws_cloudwatch_log_group.order_fulfillment_worker,
  ]

  tags = {
    Name      = var.worker_function_name
    component = "order-fulfillment"
  }
}

resource "aws_lambda_event_source_mapping" "order_fulfillment" {
  event_source_arn                   = aws_sqs_queue.order_fulfillment.arn
  function_name                      = aws_lambda_function.order_fulfillment_worker.arn
  batch_size                         = var.worker_batch_size
  maximum_batching_window_in_seconds = 5
  enabled                            = true
  function_response_types            = ["ReportBatchItemFailures"]
}
