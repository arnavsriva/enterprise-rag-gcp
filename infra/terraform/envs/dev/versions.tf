terraform {
  # >= 1.11: ephemeral values and write-only arguments keep the DB password out of state.
  required_version = ">= 1.11.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.5"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  # Partial configuration — values supplied via `-backend-config=backend.hcl`.
  backend "gcs" {}
}

provider "google" {
  project                         = var.project_id
  region                          = var.region
  default_labels                  = local.labels
  add_terraform_attribution_label = true
}

# The Budgets API needs a quota project when called with user credentials.
provider "google" {
  alias                 = "billing"
  project               = var.project_id
  billing_project       = var.project_id
  user_project_override = true
}
