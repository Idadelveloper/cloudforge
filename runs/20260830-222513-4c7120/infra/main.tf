provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project
      "managed-by" = var.managed_by
    }
  }
}

# ---------------------------------------------------------------------------
# DynamoDB: primary persistence for to-do task records (plan resource "tasks")
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "tasks" {
  name         = var.tasks_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = var.tasks_table_hash_key

  attribute {
    name = var.tasks_table_hash_key
    type = "S"
  }

  # Checkov CKV_AWS_28 - backups / point in time recovery
  point_in_time_recovery {
    enabled = true
  }

  # Checkov CKV_AWS_119 - encryption at rest
  server_side_encryption {
    enabled = true
  }

  tags = {
    Name      = var.tasks_table_name
    component = "persistence"
  }
}

# ---------------------------------------------------------------------------
# IAM: role + least-privilege policy for the backend service
# (plan resource "todo_api_app_role")
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowComputeServicesToAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type = "Service"
      identifiers = [
        "ec2.amazonaws.com",
        "lambda.amazonaws.com",
      ]
    }
  }
}

resource "aws_iam_role" "app" {
  name                 = var.app_role_name
  description          = "Role assumed by the todo_api FastAPI service to access the tasks DynamoDB table."
  assume_role_policy   = data.aws_iam_policy_document.app_assume_role.json
  max_session_duration = 3600

  tags = {
    Name      = var.app_role_name
    component = "application"
  }
}

data "aws_iam_policy_document" "tasks_table_access" {
  statement {
    sid    = "TasksTableItemAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.tasks.arn,
      "${aws_dynamodb_table.tasks.arn}/index/*",
    ]
  }
}

resource "aws_iam_policy" "tasks_table_access" {
  name        = var.app_policy_name
  description = "Least-privilege access to the todo_api tasks DynamoDB table only."
  policy      = data.aws_iam_policy_document.tasks_table_access.json

  tags = {
    Name      = var.app_policy_name
    component = "application"
  }
}

resource "aws_iam_role_policy_attachment" "app_tasks_table_access" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.tasks_table_access.arn
}
