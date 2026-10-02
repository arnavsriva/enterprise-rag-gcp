# Vertex AI Vector Search: a streaming-update index, a Private Service Connect endpoint,
# and (only when `deployed = true`) the deployed index plus the PSC forwarding rule.
#
# COST: the deployed index bills per node-hour while deployed, even when idle. The index
# and the empty endpoint do not. Keep `deployed = false` except during work sessions.
# See docs/adr/0001-dual-vector-backends.md and 0002-private-networking.md.

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name_prefix" {
  type = string
}

variable "network_name" {
  description = "VPC name (for the PSC forwarding rule)."
  type        = string
}

variable "subnet_id" {
  description = "Subnet in which the PSC endpoint IP is reserved."
  type        = string
}

variable "dimensions" {
  description = "Embedding dimension; must match the embedding model and the pgvector schema."
  type        = number
  default     = 768
}

variable "deployed" {
  description = "Deploy the index (BILLABLE per node-hour) and create the PSC endpoint."
  type        = bool
  default     = false
}

variable "machine_type" {
  description = "Node type for the deployed index. e2-standard-2 is the smallest; valid with SHARD_SIZE_SMALL."
  type        = string
  default     = "e2-standard-2"
}

variable "labels" {
  type    = map(string)
  default = {}
}

locals {
  # Deployed index IDs: letters, digits and underscores, starting with a letter.
  deployed_index_id = replace("${var.name_prefix}_deployed", "-", "_")
}

resource "google_vertex_ai_index" "this" {
  project             = var.project_id
  region              = var.region
  display_name        = "${var.name_prefix}-index"
  index_update_method = "STREAM_UPDATE"
  labels              = var.labels

  metadata {
    config {
      dimensions                  = var.dimensions
      approximate_neighbors_count = 50
      # Embeddings are L2-normalised, so dot product ranks identically to cosine (pgvector side).
      distance_measure_type = "DOT_PRODUCT_DISTANCE"
      feature_norm_type     = "UNIT_L2_NORM"
      shard_size            = "SHARD_SIZE_SMALL"

      algorithm_config {
        tree_ah_config {
          leaf_node_embedding_count    = 500
          leaf_nodes_to_search_percent = 7
        }
      }
    }
  }
}

resource "google_vertex_ai_index_endpoint" "this" {
  project      = var.project_id
  region       = var.region
  display_name = "${var.name_prefix}-endpoint"
  labels       = var.labels

  private_service_connect_config {
    enable_private_service_connect = true
    project_allowlist              = [var.project_id]
  }
}

resource "google_vertex_ai_index_endpoint_deployed_index" "this" {
  count = var.deployed ? 1 : 0

  region            = var.region
  index_endpoint    = google_vertex_ai_index_endpoint.this.id
  index             = google_vertex_ai_index.this.id
  deployed_index_id = local.deployed_index_id
  display_name      = "${var.name_prefix}-deployed"

  dedicated_resources {
    min_replica_count = 1
    max_replica_count = 1
    machine_spec {
      machine_type = var.machine_type
    }
  }

  timeouts {
    create = "90m" # first deployment commonly takes 20-60 minutes
  }
}

resource "google_compute_address" "psc" {
  count = var.deployed ? 1 : 0

  project      = var.project_id
  region       = var.region
  name         = "${var.name_prefix}-vs-psc-ip"
  address_type = "INTERNAL"
  subnetwork   = var.subnet_id
}

resource "google_compute_forwarding_rule" "psc" {
  count = var.deployed ? 1 : 0

  project               = var.project_id
  region                = var.region
  name                  = "${var.name_prefix}-vs-psc"
  network               = var.network_name
  ip_address            = google_compute_address.psc[0].id
  target                = google_vertex_ai_index_endpoint_deployed_index.this[0].private_endpoints[0].service_attachment
  load_balancing_scheme = ""
}

output "index_id" {
  description = "Numeric index ID."
  value       = google_vertex_ai_index.this.name
}

output "index_endpoint_id" {
  description = "Numeric index endpoint ID."
  value       = google_vertex_ai_index_endpoint.this.name
}

output "deployed_index_id" {
  value = var.deployed ? local.deployed_index_id : ""
}

output "psc_ip" {
  description = "Private IP to query the deployed index from inside the VPC."
  value       = var.deployed ? google_compute_address.psc[0].address : ""
}
