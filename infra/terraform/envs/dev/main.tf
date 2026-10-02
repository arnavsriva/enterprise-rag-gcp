# Dev environment composition. Every resource here is destroyed by `make down`.

locals {
  name_prefix = "rag-${var.env}"
  labels = merge(
    { project = "enterprise-rag", env = var.env, managed-by = "terraform" },
    var.labels,
  )

  budget_enabled = var.billing_account_id != ""

  services = concat([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ], local.budget_enabled ? ["billingbudgets.googleapis.com"] : [])
}

data "google_project" "this" {
  project_id = var.project_id
}

module "project_services" {
  source     = "../../modules/project_services"
  project_id = var.project_id
  services   = toset(local.services)
}

module "network" {
  source      = "../../modules/network"
  project_id  = var.project_id
  region      = var.region
  name_prefix = local.name_prefix

  depends_on = [module.project_services]
}

module "storage" {
  source     = "../../modules/storage"
  project_id = var.project_id
  region     = var.region
  name       = "${var.project_id}-${local.name_prefix}"
  labels     = local.labels

  depends_on = [module.project_services]
}

module "artifact_registry" {
  source        = "../../modules/artifact_registry"
  project_id    = var.project_id
  region        = var.region
  repository_id = local.name_prefix
  labels        = local.labels

  depends_on = [module.project_services]
}

module "iam" {
  source      = "../../modules/iam"
  project_id  = var.project_id
  name_prefix = local.name_prefix
  bucket_name = module.storage.name

  depends_on = [module.project_services]
}

module "cloud_sql" {
  source           = "../../modules/cloud_sql"
  project_id       = var.project_id
  region           = var.region
  name_prefix      = local.name_prefix
  network_id       = module.network.network_id
  tier             = var.cloud_sql_tier
  secret_accessors = [module.iam.members["api"], module.iam.members["ingest"], module.iam.members["eval"]]
  labels           = local.labels

  # Private IP requires the Private Service Access peering to exist first.
  depends_on = [module.network]
}

module "vector_search" {
  source       = "../../modules/vector_search"
  project_id   = var.project_id
  region       = var.region
  name_prefix  = local.name_prefix
  network_name = module.network.network_name
  subnet_id    = module.network.subnet_id
  dimensions   = var.embedding_dim
  deployed     = var.vector_search_deployed
  labels       = local.labels

  depends_on = [module.project_services]
}

locals {
  # Shared runtime configuration (see common/config.py for meaning and defaults).
  app_env = {
    GCP_PROJECT_ID                  = var.project_id
    GCP_REGION                      = var.region
    GCS_BUCKET                      = module.storage.name
    EMBEDDING_DIM                   = tostring(var.embedding_dim)
    RETRIEVAL_BACKEND               = var.vector_search_deployed ? "vertex_vector_search" : "pgvector"
    VECTOR_SEARCH_INDEX_ID          = module.vector_search.index_id
    VECTOR_SEARCH_INDEX_ENDPOINT_ID = module.vector_search.index_endpoint_id
    VECTOR_SEARCH_DEPLOYED_INDEX_ID = module.vector_search.deployed_index_id
    VECTOR_SEARCH_PSC_IP            = module.vector_search.psc_ip
    PG_HOST                         = module.cloud_sql.private_ip
    PG_PORT                         = "5432"
    PG_DATABASE                     = module.cloud_sql.database_name
    PG_USER                         = module.cloud_sql.user_name
    PG_SSLMODE                      = "require"
    LOG_LEVEL                       = "INFO"
  }
  app_secret_env = {
    PG_PASSWORD = module.cloud_sql.password_secret_id
  }
}

module "api" {
  source                = "../../modules/cloud_run_service"
  project_id            = var.project_id
  region                = var.region
  name                  = "${local.name_prefix}-api"
  image                 = var.placeholder_service_image
  service_account_email = module.iam.emails["api"]
  network_id            = module.network.network_id
  subnet_id             = module.network.subnet_id
  env                   = local.app_env
  secret_env            = local.app_secret_env
  invokers              = concat([module.iam.members["eval"]], var.api_invokers)
  labels                = local.labels

  depends_on = [module.cloud_sql]
}

module "ingest_job" {
  source                = "../../modules/cloud_run_job"
  project_id            = var.project_id
  region                = var.region
  name                  = "${local.name_prefix}-ingest"
  image                 = var.placeholder_job_image
  service_account_email = module.iam.emails["ingest"]
  network_id            = module.network.network_id
  subnet_id             = module.network.subnet_id
  env                   = merge(local.app_env, { SEC_USER_AGENT = var.sec_user_agent })
  secret_env            = local.app_secret_env
  labels                = local.labels

  depends_on = [module.cloud_sql]
}

module "budget" {
  count  = local.budget_enabled ? 1 : 0
  source = "../../modules/budget"
  providers = {
    google = google.billing
  }

  billing_account_id = var.billing_account_id
  project_number     = data.google_project.this.number
  display_name       = "${local.name_prefix} budget"
  amount             = var.budget_amount

  depends_on = [module.project_services]
}
