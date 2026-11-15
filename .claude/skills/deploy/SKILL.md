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
1. **Credential preflight** (no secrets printed): region and account must come from `artifacts/deploy-target.md` (`aws_region`, `aws_account`; written by the orchestrator from the requester's answer, never into the frozen `source.md`); check with `aws sts get-caller-identity` that credentials exist and match. Missing/expired/ambiguous → `needs_human` with `open_questions` (the orchestrator asks and resumes). Never guess or default a region; never store credentials.
2. `terraform init` + `terraform plan -out=tfplan`; write the plan summary (resources, estimated cost, destroy list) into `deploy-report.md` and set `needs_human` for gate `cloud-apply`.
3. Only when the orchestrator says `GATE cloud-apply APPROVED`: `terraform apply tfplan` (no `-auto-approve` needed with a saved plan), health-check, record outputs, tag with the run id, write cleanup/rollback steps.
4. `terraform destroy` or deleting resources needs gate `destructive`.
Exit criteria: `preflight_ok`, `plan_reviewed_by_human`, `applied_only_after_gate`, `health_check_passed`, `no_secrets_in_files`.

## Release rules (T51-T53)
- **T51 production release**: a release to production or any non-test environment is blocked unless the release candidate has recorded verification (`verify` passed or a recorded waiver) AND the `cloud-apply` gate (plus `destructive` where it applies) is approved. A blocked production release never blocks a deploy to a test/local environment: deploy there, record `production: blocked (unverified)` in `deploy-report.md`.
- **T52 missing account/region/credentials**: never guess, default, infer from the shell history, or persist them. Ask the requester (`needs_human` with `open_questions`); the answer goes to `artifacts/deploy-target.md` only (never `source.md`, never config, never a file under version control). Never print secret values.
- **T53 local release evidence**: deploy exactly the candidate recorded in `build-report.md` (commit SHA or artifact hash, quoted in `deploy-report.md`). Record the health check URL and result, the PID, the port, and the stop command. Cleanup stops only that recorded PID after checking that it is still that process and the port is the recorded one; never `kill` by name or pattern. If the PID is gone, say so and do nothing.

## Side-effect journal (F13)
Before any external write (Jira create/transition/comment, deploy apply) run `ghe_tool.py journal-intent --run $RUN --key <stable-key> --action <what>`; after it succeeds run `journal-confirm --key <stable-key> --evidence <id/url>`. If the intent is already `applied`, skip the write. If it is unresolved (crash, lost response, ambiguous apply) the runner refuses a retry: check the real system, then `journal-reconcile --key <k> --outcome applied|not_applied --evidence <what you checked>`. `finish --status DONE` is refused while an intent is unresolved.

## Runner-enforced release controls (T51-T53)
- Before any production release run `release-check --env production`; exit 1 means blocked (no verification/waiver, or `cloud-apply` not approved). `--env test` is always allowed.
- Run `deploy-target` before a cloud deploy: it validates `artifacts/deploy-target.md` (account, region, `provided_by: requester`) and refuses credentials. Exit 2 = ask the requester.
- Local release: `release-record --candidate <sha|hash> --health passed|failed --health-url --pid --port`, and stop it later only with `release-cleanup` (kills the recorded PID only, never by name).
