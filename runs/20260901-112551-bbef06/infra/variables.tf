variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name applied as a default tag on every resource."
  type        = string
  default     = "iot-telemetry-backend"
}

variable "devices_table_name" {
  description = "Name of the DynamoDB device registry table."
  type        = string
  default     = "iot-devices"
}

variable "readings_table_name" {
  description = "Name of the DynamoDB temperature readings table."
  type        = string
  default     = "iot-readings"
}

variable "alerts_topic_name" {
  description = "Name of the SNS topic that receives threshold breach alerts."
  type        = string
  default     = "iot-temperature-alerts"
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the telemetry backend service."
  type        = string
  default     = "iot-telemetry-app-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group used for application and alert-publication logs."
  type        = string
  default     = "/iot-telemetry/app-logs"
}

variable "log_retention_days" {
  description = "Retention period (days) for the application log group."
  type        = number
  default     = 365
}
