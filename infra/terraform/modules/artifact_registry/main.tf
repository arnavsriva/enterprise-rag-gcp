# Docker repository for the API and ingestion images.

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "repository_id" {
  type = string
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "writers" {
  description = "IAM members allowed to push images (the Cloud Build service account)."
  type        = list(string)
  default     = []
}

resource "google_artifact_registry_repository" "this" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  format        = "DOCKER"
  labels        = var.labels

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "1209600s" # 14 days
    }
  }
}

variable "readers" {
  description = "IAM members allowed to pull images (e.g. the Vertex AI custom-code service agent)."
  type        = list(string)
  default     = []
}

resource "google_artifact_registry_repository_iam_member" "reader" {
  for_each = toset(var.readers)

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.reader"
  member     = each.value
}

resource "google_artifact_registry_repository_iam_member" "writer" {
  for_each = toset(var.writers)

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.writer"
  member     = each.value
}

output "repository_url" {
  description = "Image prefix, e.g. us-central1-docker.pkg.dev/PROJECT/REPO."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.this.repository_id}"
}
