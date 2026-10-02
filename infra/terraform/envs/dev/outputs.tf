output "project_id" {
  value = var.project_id
}

output "region" {
  value = var.region
}

output "artifact_registry" {
  description = "Image prefix for docker push / Cloud Build."
  value       = module.artifact_registry.repository_url
}

output "build_service_account" {
  description = "Cloud Build runs as this account (make image)."
  value       = module.iam.emails["build"]
}

output "bucket" {
  value = module.storage.name
}

output "api_url" {
  value = module.api.uri
}

output "api_service" {
  value = module.api.name
}

output "ingest_job" {
  value = module.ingest_job.name
}

output "cloud_sql_instance" {
  value = module.cloud_sql.instance_name
}

output "service_accounts" {
  value = module.iam.emails
}

output "vector_search_deployed" {
  description = "Reminder: true means the index endpoint is billing per node-hour."
  value       = var.vector_search_deployed
}

output "app_env" {
  description = "Runtime config injected into Cloud Run (no secrets)."
  value       = local.app_env
}
