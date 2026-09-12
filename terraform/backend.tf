terraform {
  backend "gcs" {
    bucket = "rag-llm-langchain-tfstate"
    prefix = "terraform/state"
  }
}