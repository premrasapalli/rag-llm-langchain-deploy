terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "GCP region"
  default     = "us-central1"
}

variable "enable_gpu_pool" {
  type        = bool
  description = "Create the GPU (L4) node pool. Requires regional quota NVIDIA_L4_GPUS > 0 (GCP default is 0; request an increase)."
  default     = false
}

variable "gpu_zone" {
  type        = string
  description = "Zone that hosts NVIDIA L4 GPUs. L4 is not offered in every us-central1 zone (e.g. not -f); pick us-central1-a/b/c."
  default     = "us-central1-a"
}