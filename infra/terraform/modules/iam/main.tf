# One service account per workload, with only the roles that workload needs.
# "build" is the Cloud Build identity: logs + read build source from the bucket; push rights
# are granted on the Artifact Registry repository itself (artifact_registry module).
# Bucket access is granted on the bucket, not the project. Secret access is granted
# per secret (cloud_sql module), and Cloud Run invoker per service (cloud_run_service module).

variable "project_id" {
  type = string
}

variable "name_prefix" {
  type = string
}

variable "bucket_name" {
  type = string
}

locals {
  observability = ["roles/logging.logWriter", "roles/monitoring.metricWriter"]

  workloads = {
    api = {
      description   = "RAG API on Cloud Run"
      project_roles = concat(local.observability, ["roles/aiplatform.user", "roles/cloudtrace.agent"])
      bucket_role   = "roles/storage.objectViewer"
    }
    ingest = {
      description   = "Ingestion Cloud Run Job"
      project_roles = concat(local.observability, ["roles/aiplatform.user"])
      bucket_role   = "roles/storage.objectAdmin"
    }
    eval = {
      description   = "Evaluation pipeline (Vertex AI Pipelines)"
      project_roles = concat(local.observability, ["roles/aiplatform.user"])
      bucket_role   = "roles/storage.objectAdmin"
    }
    build = {
      description   = "Cloud Build: builds and pushes the app image"
      project_roles = ["roles/logging.logWriter"]
      bucket_role   = "roles/storage.objectViewer"
    }
  }

  project_bindings = merge([
    for wl, cfg in local.workloads : {
      for role in cfg.project_roles : "${wl}|${role}" => { workload = wl, role = role }
    }
  ]...)
}

resource "google_service_account" "this" {
  for_each = local.workloads

  project      = var.project_id
  account_id   = "${var.name_prefix}-${each.key}"
  display_name = "${var.name_prefix} ${each.key}"
  description  = each.value.description
}

resource "google_project_iam_member" "this" {
  for_each = local.project_bindings

  project = var.project_id
  role    = each.value.role
  member  = google_service_account.this[each.value.workload].member
}

resource "google_storage_bucket_iam_member" "this" {
  for_each = local.workloads

  bucket = var.bucket_name
  role   = each.value.bucket_role
  member = google_service_account.this[each.key].member
}

# Pipelines run as the eval SA; it must be allowed to act as itself.
resource "google_service_account_iam_member" "eval_act_as_self" {
  service_account_id = google_service_account.this["eval"].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.this["eval"].member
}

output "emails" {
  description = "Service account email per workload (api, ingest, eval)."
  value       = { for k, sa in google_service_account.this : k => sa.email }
}

output "members" {
  description = "IAM member string per workload, e.g. serviceAccount:..."
  value       = { for k, sa in google_service_account.this : k => sa.member }
}
