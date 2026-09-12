output "cluster_name" {
  value = google_container_cluster.genai.name
}

output "cluster_endpoint" {
  value = google_container_cluster.genai.endpoint
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/genai"
}

output "gpu_pool_id" {
  value = try(google_container_node_pool.gpu[0].name, "")
}

output "gateway_static_ip" {
  value = google_compute_global_address.genai_gateway.address
}