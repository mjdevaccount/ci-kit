# Fixture for ci-tf-seed's CI step (ci.yml). hashicorp/null is small and its version is fixed here.
provider "registry.terraform.io/hashicorp/null" {
  version = "3.2.4"
  hashes = [
    "h1:fixture-not-checked-by-ci-tf-seed=",
  ]
}
