# Changelog
## [Unreleased]

## [0.1.1]
### Changed
- `ghe-setup` now detects the project's stack (Node/front-end, Python, Go, Rust, Java, Terraform, Makefile, monorepo) and proposes matching validation and lint commands, never another ecosystem's tools. Playwright is suggested only when installed. Failing validation commands are reported, not silently kept.

## [0.1.0]
### Added
- Initial implementation: graph, runner, agents, skills, hooks, rules, templates, schemas, ghe-init, tests.
