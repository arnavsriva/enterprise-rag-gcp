variable "project_id" {
  description = "GCP project ID to deploy into. Never hardcode; set in terraform.tfvars."
  type        = string
}

variable "region" {
  description = "GCP region for all regional resources."
  type        = string
  default     = "us-central1"
}

variable "env" {
  description = "Environment name, used in resource names."
  type        = string
  default     = "dev"
}

variable "labels" {
  description = "Extra labels applied to every resource that supports them (cost attribution)."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------- cost switches

variable "vector_search_deployed" {
  description = "Deploy the Vector Search index. BILLABLE per node-hour while true, even when idle."
  type        = bool
  default     = false
}

variable "cloud_sql_tier" {
  description = "Cloud SQL machine tier."
  type        = string
  default     = "db-f1-micro"
}

variable "billing_account_id" {
  description = "Billing account ID (XXXXXX-XXXXXX-XXXXXX) for the budget alert. Empty = no budget."
  type        = string
  default     = ""
}

variable "budget_amount" {
  description = "Monthly budget alert threshold, in the billing account's currency."
  type        = number
  default     = 120
}

# ---------------------------------------------------------------- workloads

variable "api_invokers" {
  description = "Extra IAM members allowed to call the API, e.g. [\"user:you@example.com\"]."
  type        = list(string)
  default     = []
}

variable "sec_user_agent" {
  description = "SEC EDGAR User-Agent (\"Name email\"), required by SEC fair-access policy."
  type        = string
  default     = ""
}

variable "embedding_dim" {
  description = "Embedding dimension; must match common/migrations and the embedding model."
  type        = number
  default     = 768
}

variable "placeholder_service_image" {
  description = "Image used only at first create, before the real image is pushed."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "placeholder_job_image" {
  description = "Image used only at first create, before the real image is pushed."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/job"
}
