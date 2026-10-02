# Enables the Google APIs the platform needs.
# APIs are left enabled on destroy: disabling them is slow, free to keep, and can break
# other workloads sharing the project.

variable "project_id" {
  type = string
}

variable "services" {
  description = "API service names to enable, e.g. run.googleapis.com."
  type        = set(string)
}

resource "google_project_service" "this" {
  for_each = var.services

  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

output "enabled" {
  description = "Enabled service names (depend on this to order API enablement first)."
  value       = [for s in google_project_service.this : s.service]
}
