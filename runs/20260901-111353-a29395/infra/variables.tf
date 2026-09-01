variable "aws_region" {
  description = "AWS region used for all resources (LocalStack compatible)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project tag applied to every resource via provider default_tags."
  type        = string
  default     = "cloudforge"
}

variable "app_name" {
  description = "Logical application name from the shared plan."
  type        = string
  default     = "iot_telemetry_backend"
}

variable "environment" {
  description = "Deployment environment tag."
  type        = string
  default     = "dev"
}

variable "devices_table_name" {
  description = "Name of the DynamoDB device registry table (plan: iot-devices)."
  type        = string
  default     = "iot-devices"
}

variable "readings_table_name" {
  description = "Name of the DynamoDB telemetry readings table (plan: iot-readings)."
  type        = string
  default     = "iot-readings"
}

variable "alerts_topic_name" {
  description = "Name of the SNS topic used for threshold-breach alerts (plan: iot-telemetry-alerts)."
  type        = string
  default     = "iot-telemetry-alerts"
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the backend service (plan: iot-telemetry-app-role)."
  type        = string
  default     = "iot-telemetry-app-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group for application logs (plan: /cloudforge/iot-telemetry)."
  type        = string
  default     = "/cloudforge/iot-telemetry"
}

variable "log_retention_days" {
  description = "Retention in days for the application CloudWatch log group."
  type        = number
  default     = 365
}
