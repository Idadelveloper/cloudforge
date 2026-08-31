provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project    = var.project_name
      managed-by = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# DynamoDB: primary persistent storage for note items (aws_resources: notes_table)
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "notes_table" {
  name         = var.notes_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "note_id"

  attribute {
    name = "note_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Name = var.notes_table_name
  }
}

# ---------------------------------------------------------------------------
# CloudWatch: application and request logs (aws_resources: notes_api_logs)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "notes_api_logs" {
  # checkov:skip=CKV_AWS_158:LocalStack Community Edition does not support KMS-encrypted log groups; encryption at rest is provided by the platform default.
  name              = var.notes_api_log_group_name
  retention_in_days = var.log_retention_days

  tags = {
    Name = "notes_api_logs"
  }
}

# ---------------------------------------------------------------------------
# IAM: execution role granting least-privilege access to the notes table
# (aws_resources: notes_api_role)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "notes_api_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "notes_api_role" {
  name               = var.notes_api_role_name
  assume_role_policy = data.aws_iam_policy_document.notes_api_assume_role.json

  tags = {
    Name = var.notes_api_role_name
  }
}

data "aws_iam_policy_document" "notes_api_dynamodb" {
  statement {
    sid    = "NotesTableReadWrite"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem"
    ]

    resources = [
      aws_dynamodb_table.notes_table.arn,
      "${aws_dynamodb_table.notes_table.arn}/index/*"
    ]
  }
}

data "aws_iam_policy_document" "notes_api_logs" {
  statement {
    sid    = "NotesApiLogsWrite"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]

    resources = [
      aws_cloudwatch_log_group.notes_api_logs.arn,
      "${aws_cloudwatch_log_group.notes_api_logs.arn}:*"
    ]
  }
}

resource "aws_iam_role_policy" "notes_api_dynamodb" {
  name   = "${var.notes_api_role_name}-dynamodb"
  role   = aws_iam_role.notes_api_role.id
  policy = data.aws_iam_policy_document.notes_api_dynamodb.json
}

resource "aws_iam_role_policy" "notes_api_logs" {
  name   = "${var.notes_api_role_name}-logs"
  role   = aws_iam_role.notes_api_role.id
  policy = data.aws_iam_policy_document.notes_api_logs.json
}
