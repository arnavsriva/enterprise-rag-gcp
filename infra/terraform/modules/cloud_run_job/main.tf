# Cloud Run Job (batch ingestion / benchmarks) running inside the VPC, so it can reach
# private-IP Cloud SQL and the Vector Search PSC endpoint. Image is managed by the deploy
# script, like the service.

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
  type = string
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
  type    = map(string)
  default = {}
}

variable "secret_env" {
  description = "Env var name -> Secret Manager secret ID (latest version)."
  type        = map(string)
  default     = {}
}

variable "command" {
  description = "Container entrypoint override, e.g. [\"python\"]."
  type        = list(string)
  default     = null
}

variable "args" {
  description = "Arguments, e.g. [\"-m\", \"ingest.job\"]."
  type        = list(string)
  default     = null
}

variable "timeout" {
  type    = string
  default = "3600s"
}

variable "labels" {
  type    = map(string)
  default = {}
}

resource "google_cloud_run_v2_job" "this" {
  project             = var.project_id
  location            = var.region
  name                = var.name
  deletion_protection = false
  labels              = var.labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = var.service_account_email
      timeout         = var.timeout
      max_retries     = 1

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = var.network_id
          subnetwork = var.subnet_id
        }
      }

      containers {
        image   = var.image
        command = var.command
        args    = var.args

        resources {
          limits = {
            cpu    = "2"
            memory = "2Gi"
          }
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
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }
}

output "name" {
  value = google_cloud_run_v2_job.this.name
}
