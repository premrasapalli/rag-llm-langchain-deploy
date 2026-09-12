output "cluster_name" {
  value = google_container_cluster.rag_llm_langchain.name
}

output "cluster_endpoint" {
  value = google_container_cluster.rag_llm_langchain.endpoint
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/rag-llm-langchain"
}

output "gpu_pool_id" {
  value = try(google_container_node_pool.gpu[0].name, "")
}

output "gateway_static_ip" {
  value = google_compute_global_address.rag_llm_langchain_gateway.address
}