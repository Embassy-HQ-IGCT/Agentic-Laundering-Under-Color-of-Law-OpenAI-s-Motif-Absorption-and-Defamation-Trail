# SPDX-License-Identifier: Apache-2.0
output "cluster_name" { value = google_container_cluster.gke.name }
output "cluster_location" { value = google_container_cluster.gke.location }
output "sql_private_ip" { value = google_sql_database_instance.pg.private_ip_address }
output "redis_host" { value = google_redis_instance.redis.host }
output "redis_port" { value = google_redis_instance.redis.port }
output "registry" { value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}" }
output "app_service_account" { value = google_service_account.app.email }
output "db_secret_ids" { value = { for k, v in google_secret_manager_secret.db : k => v.secret_id } }
