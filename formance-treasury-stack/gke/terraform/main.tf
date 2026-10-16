locals {
  apis = [
    "container.googleapis.com", "compute.googleapis.com", "sqladmin.googleapis.com",
    "redis.googleapis.com", "secretmanager.googleapis.com", "servicenetworking.googleapis.com",
    "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "iam.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- network
resource "google_compute_network" "vpc" {
  name                    = "${var.name}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "subnet" {
  name                     = "${var.name}-subnet"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }
}

resource "google_compute_router" "router" {
  name    = "${var.name}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  name                               = "${var.name}-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_compute_global_address" "private_services" {
  name          = "${var.name}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
}

# ---------------------------------------------------------------- GKE (Autopilot)
resource "google_container_cluster" "gke" {
  name                = var.name
  location            = var.region
  enable_autopilot    = true
  network             = google_compute_network.vpc.id
  subnetwork          = google_compute_subnetwork.subnet.id
  deletion_protection = var.deletion_protection

  release_channel {
    channel = "REGULAR"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = var.master_cidr
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.master_authorized_cidrs
      content {
        cidr_block   = cidr_blocks.value.cidr
        display_name = cidr_blocks.value.name
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- Cloud SQL (Postgres, private IP)
resource "random_id" "sql_suffix" {
  byte_length = 3
}

resource "google_sql_database_instance" "pg" {
  name                = "${var.name}-pg-${random_id.sql_suffix.hex}"
  region              = var.region
  database_version    = "POSTGRES_15"
  deletion_protection = var.deletion_protection

  settings {
    tier              = var.sql_tier
    availability_type = var.sql_high_availability ? "REGIONAL" : "ZONAL"
    disk_autoresize   = true
    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
    }
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }
  }

  depends_on = [google_service_networking_connection.private_services]
}

resource "random_password" "db" {
  for_each = toset(var.databases)
  length   = 32
  special  = false
}

resource "google_sql_database" "db" {
  for_each = toset(var.databases)
  name     = each.value
  instance = google_sql_database_instance.pg.name
}

resource "google_sql_user" "db" {
  for_each = toset(var.databases)
  name     = each.value
  instance = google_sql_database_instance.pg.name
  password = random_password.db[each.value].result
}

# ---------------------------------------------------------------- Memorystore Redis
resource "google_redis_instance" "redis" {
  name               = "${var.name}-redis"
  region             = var.region
  tier               = var.redis_tier
  memory_size_gb     = var.redis_memory_gb
  redis_version      = "REDIS_7_0"
  authorized_network = google_compute_network.vpc.id
  connect_mode       = "PRIVATE_SERVICE_ACCESS"
  depends_on         = [google_service_networking_connection.private_services]
}

# ---------------------------------------------------------------- Artifact Registry
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "${var.name}-images"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# ---------------------------------------------------------------- secrets
resource "google_secret_manager_secret" "db" {
  for_each  = toset(var.databases)
  secret_id = "${var.name}-db-${replace(each.value, "_", "-")}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db" {
  for_each    = toset(var.databases)
  secret      = google_secret_manager_secret.db[each.value].id
  secret_data = random_password.db[each.value].result
}

resource "google_secret_manager_secret" "bank" {
  for_each  = toset(var.bank_secret_names)
  secret_id = "${var.name}-bank-${each.value}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- workload identity
resource "google_service_account" "app" {
  account_id   = "${var.name}-app"
  display_name = "${var.name} workloads"
}

resource "google_project_iam_member" "app_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_service_account_iam_member" "wi" {
  service_account_id = google_service_account.app.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.k8s_service_account}]"
  depends_on         = [google_container_cluster.gke]
}
