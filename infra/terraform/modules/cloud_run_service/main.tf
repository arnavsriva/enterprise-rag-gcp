# Cloud Run service with Direct VPC egress (private ranges only) and IAM-only invocation.
#
# Terraform owns the service *configuration*. Image rollouts happen outside Terraform
# (build -> deploy a no-traffic revision -> eval gate -> shift traffic), so image and traffic
# are ignored here. See docs/adr/0004-terraform-layout-and-deploy-model.md.

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  type = string
}

variable "image" {
  description = "Initial image only; later rollouts are done by the deploy script."
  type        = string
}

variable "service_account_email" {
  type = string
}

variable "network_id" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "env" {
  description = "Plain environment variables."
  type        = map(string)
  default     = {}
}

variable "secret_env" {
  description = "Env var name -> Secret Manager secret ID (latest version is mounted)."
  type        = map(string)
  default     = {}
}

variable "invokers" {
  description = "IAM members allowed to call the service (no public access)."
  type        = list(string)
  default     = []
}

variable "max_instances" {
  type    = number
  default = 2
}

variable "labels" {
  type    = map(string)
  default = {}
}

resource "google_cloud_run_v2_service" "this" {
  project             = var.project_id
  location            = var.region
  name                = var.name
  ingress             = "INGRESS_TRAFFIC_ALL" # reachable, but every request needs an IAM identity token
  deletion_protection = false
  labels              = var.labels

  template {
    service_account                  = var.service_account_email
    max_instance_request_concurrency = 20
    timeout                          = "120s"

    scaling {
      min_instance_count = 0 # scale to zero: no idle cost
      max_instance_count = var.max_instances
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = var.network_id
        subnetwork = var.subnet_id
      }
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle = true
      }

      dynamic "env" {
        for_each = var.env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      traffic,
      client,
      client_version,
    ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "invoker" {
  for_each = toset(var.invokers)

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.this.name
  role     = "roles/run.invoker"
  member   = each.value
}

output "name" {
  value = google_cloud_run_v2_service.this.name
}

output "uri" {
  value = google_cloud_run_v2_service.this.uri
}
