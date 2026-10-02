# Cloud SQL for PostgreSQL 16 (pgvector), private IP only, password in Secret Manager.
#
# The password is generated as an *ephemeral* value and written only to write-only
# arguments, so it never appears in Terraform state or plan output.
# See docs/adr/0003-postgres-schema-and-auth.md.

terraform {
  required_providers {
    google = { source = "hashicorp/google" }
    random = { source = "hashicorp/random" }
  }
}

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name_prefix" {
  type = string
}

variable "network_id" {
  description = "VPC with Private Service Access already configured."
  type        = string
}

variable "tier" {
  description = "Machine tier. db-f1-micro is shared-core (no SLA) and the cheapest option."
  type        = string
  default     = "db-f1-micro"
}

variable "disk_size_gb" {
  type    = number
  default = 10
}

variable "database_name" {
  type    = string
  default = "rag"
}

variable "user_name" {
  type    = string
  default = "rag"
}

variable "secret_accessors" {
  description = "IAM members allowed to read the DB password secret."
  type        = list(string)
  default     = []
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "password_version" {
  description = "Bump to rotate the DB password (re-applies the write-only values)."
  type        = number
  default     = 1
}

# Cloud SQL instance names cannot be reused for ~1 week after deletion; a random suffix
# keeps `make down` / `make up` cycles working.
resource "random_id" "suffix" {
  byte_length = 3
}

ephemeral "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_database_instance" "this" {
  project             = var.project_id
  name                = "${var.name_prefix}-pg-${random_id.suffix.hex}"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = false

  settings {
    edition                     = "ENTERPRISE"
    tier                        = var.tier
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = var.disk_size_gb
    disk_autoresize             = false
    deletion_protection_enabled = false
    user_labels                 = var.labels

    ip_configuration {
      ipv4_enabled    = false
      private_network = var.network_id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled = false # dev only: corpus is fully re-ingestable
    }
  }
}

resource "google_sql_database" "this" {
  project  = var.project_id
  instance = google_sql_database_instance.this.name
  name     = var.database_name
}

resource "google_sql_user" "this" {
  project             = var.project_id
  instance            = google_sql_database_instance.this.name
  name                = var.user_name
  password_wo         = ephemeral.random_password.db.result
  password_wo_version = var.password_version
}

resource "google_secret_manager_secret" "db_password" {
  project   = var.project_id
  secret_id = "${var.name_prefix}-pg-password"
  labels    = var.labels

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret                 = google_secret_manager_secret.db_password.id
  secret_data_wo         = ephemeral.random_password.db.result
  secret_data_wo_version = var.password_version
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each = toset(var.secret_accessors)

  project   = var.project_id
  secret_id = google_secret_manager_secret.db_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value
}

output "instance_name" {
  value = google_sql_database_instance.this.name
}

output "private_ip" {
  value = google_sql_database_instance.this.private_ip_address
}

output "database_name" {
  value = google_sql_database.this.name
}

output "user_name" {
  value = google_sql_user.this.name
}

output "password_secret_id" {
  description = "Secret Manager secret holding the DB password (latest version)."
  value       = google_secret_manager_secret.db_password.secret_id
}
