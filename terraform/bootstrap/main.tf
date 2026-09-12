# Bootstrap: run ONCE before the main apply.
#
#   1. Creates the GCS bucket used as the (remote) state backend.
#   2. Enables the APIs the cluster needs.
#
# The main `terraform/` config reads bucket `genai-terraform-state` in its
# backend block, so this bucket MUST exist before `terraform init` there.
# This config deliberately has no backend block.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0, < 7.0"
    }
  }
}

variable "project_id" {
  type        = string
  description = "GCP project ID"
  default     = "aiml-project-idp"
}

variable "region" {
  type        = string
  description = "GCP region"
  default     = "us-central1"
}

variable "state_bucket" {
  type        = string
  description = "GCS bucket name for Terraform remote state"
  default     = "aiml-project-idp-genai-tfstate"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --- Remote-state bucket -----------------------------------------------------
resource "google_storage_bucket" "terraform_state" {
  name          = var.state_bucket
  location      = "US"
  force_destroy = false

  versioning {
    enabled = true
  }

  uniform_bucket_level_access = true

  lifecycle {
    prevent_destroy = true
  }
}

# --- APIs the main config depends on ------------------------------------------
resource "google_project_service" "required" {
  for_each = toset([
    "container.googleapis.com",        # GKE
    "artifactregistry.googleapis.com", # image repo (genai)
    "compute.googleapis.com",          # node VMs / load balancer / addresses
    "storage-api.googleapis.com",      # GCS / devstorage scope
    "monitoring.googleapis.com",       # alerting policies
  ])
  service            = each.key
  disable_on_destroy = false
}

output "state_bucket" {
  value = google_storage_bucket.terraform_state.name
}