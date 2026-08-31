provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "terraform"
      environment  = var.environment
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# DynamoDB - plan entry: DynamoDB / todo_tasks
# Single table keyed by the server generated task_id. Backs every
# create / list / get / update / complete / delete endpoint.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "todo_tasks" {
  # checkov:skip=CKV_AWS_119: LocalStack Community Edition target has no KMS CMK support; the AWS managed DynamoDB key is used instead.
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = var.dynamodb_hash_key

  attribute {
    name = var.dynamodb_hash_key
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = false

  ttl {
    attribute_name = ""
    enabled        = false
  }

  tags = {
    Name      = var.dynamodb_table_name
    component = "task-store"
  }
}

# ---------------------------------------------------------------------------
# CloudWatch Logs - plan entry: CloudWatch / /todo-task-api/application
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "application" {
  # checkov:skip=CKV_AWS_158: LocalStack Community Edition target has no KMS CMK support; logs use the default CloudWatch Logs encryption.
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days

  tags = {
    Name      = var.log_group_name
    component = "application-logs"
  }
}

# ---------------------------------------------------------------------------
# IAM - plan entry: IAM / todo_task_api_app_policy
# Least privilege access to exactly the one table and the one log group the
# FastAPI service uses, attached to the role whose credentials the service
# runs with.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowComputeToAssumeAppRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name                 = var.app_role_name
  description          = "Role assumed by the todo_task_api FastAPI service for DynamoDB and CloudWatch Logs access."
  assume_role_policy   = data.aws_iam_policy_document.app_assume_role.json
  max_session_duration = 3600

  tags = {
    Name      = var.app_role_name
    component = "application-identity"
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "TaskTableDataAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Scan",
      "dynamodb:Query",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.todo_tasks.arn,
    ]
  }

  statement {
    sid    = "ApplicationLogWrite"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = [
      aws_cloudwatch_log_group.application.arn,
      "${aws_cloudwatch_log_group.application.arn}:log-stream:*",
    ]
  }
}

resource "aws_iam_policy" "app" {
  name        = var.app_policy_name
  description = "Least privilege policy for the todo_task_api service: task table CRUD plus application log writes."
  policy      = data.aws_iam_policy_document.app.json

  tags = {
    Name      = var.app_policy_name
    component = "application-identity"
  }
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
