# =============================================================================
# infra/aws/terraform — outputs. Everything the human runbook (../APPLY-PLAN.md)
# and the firewall verifier (../firewall/verify_lockdown_aws.sh) need to be fed.
# =============================================================================

output "enforcement_eip" {
  description = "Elastic IP of EC2 #1 (biject-enforcement). Point ALL THREE Namecheap A records (proxy./wall./oc.<domain>) here."
  value       = aws_eip.enforcement.public_ip
}

output "agent_eip" {
  description = "Elastic IP of EC2 #2 (biject-agent). This exact /32 is what sg-enforcement admits on :443 — see main.tf header for why an SG-source rule could not do this."
  value       = aws_eip.agent.public_ip
}

output "enforcement_private_ip" {
  description = "Private IP of the enforcement host. Feed to verify_lockdown_aws.sh as ENFORCEMENT_PRIVATE_IP (the direct-:8080/:5432 probes target it)."
  value       = aws_instance.enforcement.private_ip
}

output "agent_private_ip" {
  description = "Private IP of the agent host."
  value       = aws_instance.agent.private_ip
}

output "enforcement_instance_id" {
  description = "Instance ID of EC2 #1 — needed for the post-green EBS snapshot step."
  value       = aws_instance.enforcement.id
}

output "agent_instance_id" {
  description = "Instance ID of EC2 #2."
  value       = aws_instance.agent.id
}

output "enforcement_root_volume_id" {
  description = "Root EBS volume of the enforcement host — the snapshot target after the stack is green (APPLY-PLAN step 9)."
  value       = aws_instance.enforcement.root_block_device[0].volume_id
}

output "dns_records" {
  description = "The exact A records to create in Namecheap Advanced DNS."
  value = {
    "proxy.${var.domain}" = aws_eip.enforcement.public_ip
    "wall.${var.domain}"  = aws_eip.enforcement.public_ip
    "oc.${var.domain}"    = aws_eip.enforcement.public_ip
  }
}

output "ssh_enforcement" {
  description = "Convenience: SSH to the enforcement host (admin_ip only)."
  value       = "ssh -i <path-to-${var.key_name}.pem> ubuntu@${aws_eip.enforcement.public_ip}"
}

output "ssh_agent" {
  description = "Convenience: SSH to the agent host (admin_ip only)."
  value       = "ssh -i <path-to-${var.key_name}.pem> ubuntu@${aws_eip.agent.public_ip}"
}
