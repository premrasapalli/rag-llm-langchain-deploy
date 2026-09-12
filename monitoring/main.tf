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

variable "notify_emails" {
  type        = list(string)
  description = "Email addresses for alert notifications"
  default     = []
}

# --- Notification channel (email) ------------------------------------------
resource "google_monitoring_notification_channel" "email" {
  count        = length(var.notify_emails) > 0 ? 1 : 0
  display_name = "GenAI Alert Email"
  type         = "email"
  labels = {
    email_address = var.notify_emails[0]
  }
}

locals {
  channels = [for c in google_monitoring_notification_channel.email : c.id]
}

# --- Alert: vLLM / gateway / rag pods not ready (uptime) --------------------
resource "google_monitoring_alert_policy" "genai_workload_down" {
  count = length(local.channels) > 0 ? 1 : 0

  display_name = "GenAI workload not ready (uptime)"
  combiner     = "OR"

  conditions {
    display_name = "gateway/serving-llm/rag-service availability"
    condition_threshold {
      filter          = <<-EOT
        resource.type = "k8s_container"
        AND resource.labels.namespace_name = "genai"
        AND metric.type = "kubernetes.io/anthos/container/is_ready"
        AND metric.labels.state = "false"
      EOT
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.channels

  alert_strategy {
    auto_close = "3600s"
  }
}

# --- Alert: GPU quota usage -------------------------------------------------
resource "google_monitoring_alert_policy" "gpu_quota_high" {
  count = length(local.channels) > 0 ? 1 : 0

  display_name = "GPU quota usage high"
  combiner     = "OR"

  conditions {
    display_name = "Region quota 'NVIDIA_L4_GPUS' > 80%"
    condition_threshold {
      filter          = <<-EOT
        resource.type = "consumer_quota"
        AND metric.type = "serviceruntime.googleapis.com/quota/allocation/usage"
        AND resource.label.type = "NVIDIA_L4_GPUS"
      EOT
      duration        = "600s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_MAX"
      }
      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.channels

  alert_strategy {
    auto_close = "7200s"
  }
}

# --- Alert: GPU node unhealthy -----------------------------------------------
resource "google_monitoring_alert_policy" "gpu_node_unhealthy" {
  count = length(local.channels) > 0 ? 1 : 0

  display_name = "GPU node not ready"
  combiner     = "OR"

  conditions {
    display_name = "gpu-pool node readiness"
    condition_threshold {
      filter          = <<-EOT
        resource.type = "k8s_node"
        AND metric.type = "kubernetes.io/node/ready"
        AND metric.labels.status != "true"
      EOT
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.channels
}