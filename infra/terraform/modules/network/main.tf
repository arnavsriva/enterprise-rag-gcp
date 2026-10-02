# Private networking: a custom VPC with one private subnet, plus Private Service Access
# (VPC peering) so Cloud SQL gets a private IP only. No Cloud NAT and no Serverless VPC
# Access connector: Cloud Run uses Direct VPC egress for private ranges only, so outbound
# internet (SEC EDGAR, Google APIs) leaves via Cloud Run's default path at no extra cost.
# See docs/adr/0002-private-networking.md.

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name_prefix" {
  type = string
}

variable "subnet_cidr" {
  description = "Primary range of the private subnet (Cloud Run Direct VPC egress needs at least /26)."
  type        = string
  default     = "10.10.0.0/24"
}

variable "psa_prefix_length" {
  description = "Size of the range reserved for Google-managed services (Cloud SQL)."
  type        = number
  default     = 20
}

resource "google_compute_network" "vpc" {
  project                 = var.project_id
  name                    = "${var.name_prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "private" {
  project                  = var.project_id
  name                     = "${var.name_prefix}-private"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_global_address" "psa_range" {
  project       = var.project_id
  name          = "${var.name_prefix}-psa-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.psa_prefix_length
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa_range.name]

  # Deleting the peering often fails while Cloud SQL is still releasing it; abandoning it
  # lets `make down` finish, and it is removed together with the VPC.
  deletion_policy = "ABANDON"
}

output "network_id" {
  value = google_compute_network.vpc.id
}

output "network_name" {
  value = google_compute_network.vpc.name
}

output "subnet_id" {
  value = google_compute_subnetwork.private.id
}

output "subnet_name" {
  value = google_compute_subnetwork.private.name
}

output "psa_connection_id" {
  description = "Depend on this before creating private-IP Cloud SQL."
  value       = google_service_networking_connection.psa.id
}
