# Billing budget alert for this project.
#
# Credits are EXCLUDED so alerts track gross spend: with the default (credits included),
# net cost stays ~$0 while free-trial credits last and the alerts would never fire.
# Alerts email billing account admins; they do not stop spending.

terraform {
  required_providers {
    google = { source = "hashicorp/google" }
  }
}

variable "billing_account_id" {
  type = string
}

variable "project_number" {
  type = string
}

variable "display_name" {
  type = string
}

variable "amount" {
  description = "Monthly budget in the billing account's currency (whole units)."
  type        = number
}

resource "google_billing_budget" "this" {
  billing_account = var.billing_account_id
  display_name    = var.display_name

  budget_filter {
    projects               = ["projects/${var.project_number}"]
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      units = tostring(var.amount)
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.25, 0.5, 0.75, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
    }
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }
}
