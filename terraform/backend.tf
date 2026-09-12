terraform {
  backend "gcs" {
    bucket = "aiml-project-idp-genai-tfstate"
    prefix = "terraform/state"
  }
}