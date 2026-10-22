variable "cluster_name" {
  type = string
}

variable "kubernetes_version" {
  description = "EKS version (null = the default for new clusters)"
  type        = string
  default     = null
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "public_access_cidrs" {
  description = "Who may reach the Kubernetes API endpoint (GitHub-hosted runners have no fixed IPs)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "pipeline_role_name" {
  description = "The GitHub Actions role from infra/bootstrap: always a cluster admin"
  type        = string
  default     = "agripulse-eks-github-actions"
}

variable "admin_arns" {
  description = "Extra IAM users/roles with cluster admin (e.g. your own IAM user for kubectl)"
  type        = list(string)
  default     = []
}

variable "node_instance_types" {
  type    = list(string)
  default = ["t3.large"]
}

variable "node_min" {
  type    = number
  default = 2
}

variable "node_desired" {
  type    = number
  default = 3
}

variable "node_max" {
  type    = number
  default = 4
}

variable "pod_identities" {
  description = "Role per ServiceAccount: key => { namespace, service_account, managed_policy_arns, inline_policy (JSON) }"
  type = map(object({
    namespace           = string
    service_account     = string
    managed_policy_arns = list(string)
    inline_policy       = string
  }))
  default = {}
}
