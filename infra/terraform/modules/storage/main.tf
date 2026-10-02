# Bucket for raw filings, chunk exports, and the Vertex AI Pipelines root.

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  description = "Globally unique bucket name."
  type        = string
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "force_destroy" {
  description = "Delete all objects on destroy, so `make down` leaves nothing behind."
  type        = bool
  default     = true
}

resource "google_storage_bucket" "this" {
  project                     = var.project_id
  name                        = var.name
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy
  labels                      = var.labels

  # No soft-delete retention: deleted objects stop billing immediately in this dev env.
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["pipeline_root/"]
    }
    action {
      type = "Delete"
    }
  }
}

output "name" {
  value = google_storage_bucket.this.name
}

output "url" {
  value = google_storage_bucket.this.url
}
