---
name: deploy
description: DevOps procedure for prep (IaC authoring/validation) and deploy (local run or Terraform on AWS) behind human gates.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, AskUserQuestion
---
# deploy — DevOps node procedure (spec §7.6)

## PHASE=prep (parallel with Builder; never applies)
Write IaC / start scripts where the LLD says. `terraform fmt -check`, `terraform validate`, `tflint` if installed. Write `infra-prep-report.md`. Exit criteria: `iac_validated_or_not_applicable`, `nothing_applied`.

## PHASE=deploy, local
Build per `build-report.md`, start the app (background), poll the health-check URL (timeout 60s), write `deploy-report.md` (URL, ports, PID, stop command). Exit criteria: `health_check_passed`, `report_written`.

## PHASE=deploy, cloud
1. **Credential preflight** (no secrets printed): region and account must come from `source.md` (`aws_region`, `aws_account`) or the requester's answer; check with `aws sts get-caller-identity` that credentials exist and match. Missing/expired/ambiguous → `needs_human` with `open_questions` (the orchestrator asks and resumes). Never guess or default a region; never store credentials.
2. `terraform init` + `terraform plan -out=tfplan`; write the plan summary (resources, estimated cost, destroy list) into `deploy-report.md` and set `needs_human` for gate `cloud-apply`.
3. Only when the orchestrator says `GATE cloud-apply APPROVED`: `terraform apply tfplan` (no `-auto-approve` needed with a saved plan), health-check, record outputs, tag with the run id, write cleanup/rollback steps.
4. `terraform destroy` or deleting resources needs gate `destructive`.
Exit criteria: `preflight_ok`, `plan_reviewed_by_human`, `applied_only_after_gate`, `health_check_passed`, `no_secrets_in_files`.
