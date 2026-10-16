# SPDX-License-Identifier: Apache-2.0
variable "project_id" { type = string }
variable "region" { type = string }
variable "name" {
  type        = string
  description = "Cluster / resource name prefix"
  default     = "treasury"
}
variable "namespace" {
  type    = string
  default = "treasury"
}
variable "k8s_service_account" {
  type    = string
  default = "treasury-app"
}
variable "subnet_cidr" {
  type    = string
  default = "10.10.0.0/20"
}
variable "pods_cidr" {
  type    = string
  default = "10.20.0.0/16"
}
variable "services_cidr" {
  type    = string
  default = "10.30.0.0/20"
}
variable "master_cidr" {
  type    = string
  default = "172.16.0.0/28"
}
variable "master_authorized_cidrs" {
  type        = list(object({ cidr = string, name = string }))
  description = "Networks allowed to reach the GKE control plane (your admin / CI egress ranges)"
  default     = []
}
variable "sql_tier" {
  type    = string
  default = "db-custom-2-7680"
}
variable "sql_high_availability" {
  type    = bool
  default = true
}
variable "redis_tier" {
  type    = string
  default = "STANDARD_HA"
}
variable "redis_memory_gb" {
  type    = number
  default = 2
}
variable "deletion_protection" {
  type    = bool
  default = true
}
variable "databases" {
  type        = list(string)
  description = "Logical Postgres databases (each gets its own user and Secret Manager password)"
  default     = ["rafiki_backend", "rafiki_auth", "payments", "temporal", "temporal_visibility", "treasury"]
}
variable "bank_secret_names" {
  type        = list(string)
  description = "Empty Secret Manager secrets created for bank / PSP credentials; you add the versions"
  default     = []
}
