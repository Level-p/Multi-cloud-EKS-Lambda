variable "bindings" {
  description = "Target group (aws stack) => the Service whose pods fill it: { name, namespace, service, port, targetGroupArn }"
  type = list(object({
    name           = string
    namespace      = string
    service        = string
    port           = number
    targetGroupArn = string
  }))
}
