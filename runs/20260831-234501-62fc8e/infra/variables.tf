variable "aws_region" {
  description = "AWS region used for all resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied to resource names and tags."
  type        = string
  default     = "image-gallery"
}

variable "managed_by" {
  description = "Value for the managed-by default tag."
  type        = string
  default     = "cloudforge-terraform"
}

variable "media_bucket_name" {
  description = "Name of the S3 bucket holding image bytes (plan: image-gallery-media)."
  type        = string
  default     = "image-gallery-media"
}

variable "logs_bucket_name" {
  description = "Name of the S3 bucket that receives server access logs for the media bucket."
  type        = string
  default     = "image-gallery-media-access-logs"
}

variable "albums_table_name" {
  description = "DynamoDB table storing album metadata (plan: image-gallery-albums)."
  type        = string
  default     = "image-gallery-albums"
}

variable "images_table_name" {
  description = "DynamoDB table storing image metadata (plan: image-gallery-images)."
  type        = string
  default     = "image-gallery-images"
}

variable "app_role_name" {
  description = "IAM role assumed by the backend service (plan: image-gallery-app-role)."
  type        = string
  default     = "image-gallery-app-role"
}

variable "app_role_trusted_service" {
  description = "AWS service principal permitted to assume the application role."
  type        = string
  default     = "ec2.amazonaws.com"
}

variable "log_group_name" {
  description = "CloudWatch Logs group for application logs (plan: /cloudforge/image-gallery)."
  type        = string
  default     = "/cloudforge/image-gallery"
}

variable "log_retention_days" {
  description = "Retention period, in days, for the application log group."
  type        = number
  default     = 365
}

variable "app_config_secret_name" {
  description = "Secrets Manager secret holding runtime configuration (plan: image-gallery/app-config)."
  type        = string
  default     = "image-gallery/app-config"
}

variable "presigned_url_ttl_seconds" {
  description = "Lifetime of presigned S3 upload/download URLs issued by the API."
  type        = number
  default     = 900
}

variable "s3_object_prefix" {
  description = "Key prefix under which album images are stored (albums/{album_id}/{image_id}/{filename})."
  type        = string
  default     = "albums"
}

variable "cors_allowed_origins" {
  description = "Origins allowed to upload/download image bytes directly from the browser."
  type        = list(string)
  default     = ["*"]
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which noncurrent S3 object versions are expired."
  type        = number
  default     = 90
}

variable "access_log_expiration_days" {
  description = "Days after which S3 access log objects are expired."
  type        = number
  default     = 90
}
