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
  description = "Labels applied to every resource that supports them (used for cost attribution)."
  type        = map(string)
  default     = {}
}
