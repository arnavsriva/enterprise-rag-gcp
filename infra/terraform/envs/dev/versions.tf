terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Partial configuration — values supplied via `-backend-config=backend.hcl`.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
