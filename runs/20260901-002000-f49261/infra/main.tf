provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "cloudforge-terraform"
      environment  = var.environment
    }
  }
}

#############################################
# DynamoDB: expense records + category GSI
#############################################

resource "aws_dynamodb_table" "expenses" {
  # checkov:skip=CKV_AWS_119:LocalStack Community deployment target; AWS-owned DynamoDB encryption is used because KMS CMKs are outside the permitted service list for this environment.
  name         = var.expenses_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "sk"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "gsi1pk"
    type = "S"
  }

  # gsi1pk = "<user_id>#<category>", sk = "<date>#<expense_id>"
  # Supports GET /expenses?category=... and the per-category monthly summary
  # as range queries instead of table scans.
  global_secondary_index {
    name            = var.category_index_name
    hash_key        = "gsi1pk"
    range_key       = "sk"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Name      = var.expenses_table_name
    component = "persistence"
  }
}

#############################################
# CloudWatch Logs: application log group
#############################################

resource "aws_cloudwatch_log_group" "app" {
  # checkov:skip=CKV_AWS_158:LocalStack Community deployment target; KMS CMK encryption of log groups is outside the permitted service list for this environment.
  name              = var.log_group_name
  retention_in_days = var.log_retention_days

  tags = {
    Name      = var.log_group_name
    component = "observability"
  }
}

#############################################
# IAM: least-privilege role for the service
#############################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowComputeToAssumeExpenseTrackerRole"
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
  description          = "Role assumed by the expense tracker FastAPI service to access its DynamoDB table and log group."
  assume_role_policy   = data.aws_iam_policy_document.app_assume_role.json
  max_session_duration = 3600

  tags = {
    Name      = var.app_role_name
    component = "iam"
  }
}

data "aws_iam_policy_document" "dynamodb_access" {
  statement {
    sid    = "ExpenseItemAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.expenses.arn,
    ]
  }

  statement {
    sid    = "ExpenseCategoryIndexQuery"
    effect = "Allow"

    actions = [
      "dynamodb:Query",
    ]

    resources = [
      "${aws_dynamodb_table.expenses.arn}/index/${var.category_index_name}",
    ]
  }
}

resource "aws_iam_policy" "dynamodb_access" {
  name        = var.dynamodb_policy_name
  description = "Least-privilege DynamoDB item and query access for the expense tracker API."
  policy      = data.aws_iam_policy_document.dynamodb_access.json

  tags = {
    Name      = var.dynamodb_policy_name
    component = "iam"
  }
}

resource "aws_iam_role_policy_attachment" "dynamodb_access" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.dynamodb_access.arn
}

data "aws_iam_policy_document" "logging" {
  statement {
    sid    = "WriteApplicationLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = [
      aws_cloudwatch_log_group.app.arn,
      "${aws_cloudwatch_log_group.app.arn}:log-stream:*",
    ]
  }
}

resource "aws_iam_policy" "logging" {
  name        = "${var.project_name}-logging-policy"
  description = "Allows the expense tracker API to write structured logs to its own CloudWatch log group only."
  policy      = data.aws_iam_policy_document.logging.json

  tags = {
    Name      = "${var.project_name}-logging-policy"
    component = "iam"
  }
}

resource "aws_iam_role_policy_attachment" "logging" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.logging.arn
}
