# Changelog

## 1.23.10 (2026-10-??)

### Added

* Add configurable read-write Docker shares.
* Add configurable PostgreSQL version to the local Docker workflow.

### Changed

* Show verbose output during Docker database restores.

## 1.23.9 (2026-10-06)

### Added

* Allow Unix Docker helpers to run Docker through `sudo` via `ODOO_DOCKER_SUDO=1`.

## 1.23.8 (2026-10-06)

### Fixed

* Prevent Docker helpers from rebuilding or starting dependencies.

## 1.23.7 (2026-10-06)

### Added

* Add optional Mailpit support to the local Docker workflow.

## 1.23.6 (2026-10-05)

### Fixed

* Ensure Docker run, shell, and update helpers build the Odoo image when needed and start required dependencies.

## 1.23.5 (2026-10-05)

### Added

* Add Docker run helper.

## 1.23.4 (2026-10-05)

### Added

* Add Docker shell and database update helpers.

## 1.23.3 (2026-10-03)

### Documentation

- Update Usage.

## 1.23.2 (2026-10-03)

### Added

* Add `--init` as the preferred project initialization option while keeping `--init-project` as a compatibility alias.

## 1.23.1 (2026-10-03)

### Added

* Add `--copy` / `--move` semantics to generated Docker database restore helpers.
* Add Docker database neutralization helpers.

## 1.23.0 (2026-09-29)

### Added

* Add `odoo-compose` as the primary CLI command while keeping `odt-env` as a backward-compatible alias.

## 1.22.0 (2026-09-29)

### Added

* Add Odoo 20.0 support.

## 1.21.4 (2026-09-27)

### Changed

* Require `--force` when restoring over an existing Docker database or filestore.

## 1.21.3 (2026-09-27)

### Fixed

* Prevent base-image Odoo code from mixing with workspace Odoo in Docker.

## 1.21.2 (2026-09-26)

### Added

* Log the version at startup and in the summary report.

## 1.21.1 (2026-09-26)

### Added

* Add `[virtualenv].constraints`.

## 1.21.0 (2026-09-25)

### Added

* Allow `--set` to add supported project options.
* Add Docker support for running Odoo from workspace sources.

## 1.20.5 (2026-09-25)

### Changed

* Install Restic from the official Docker image instead of APT for compatibility with older Odoo base images.

## 1.20.4 (2026-09-24)

### Documentation

* Reorganize README sections

## 1.20.3 (2026-09-24)

### Documentation

* Reorganize README around Docker and native venv workflows.

## 1.20.2 (2026-09-23)

### Changed

* Configure explicit log rotation for Local Docker Odoo and PostgreSQL services.

## 1.20.1 (2026-09-23)

### Documentation

- Improve README section numbering.

## 1.20.0 (2026-09-23)

### Added

- Generate Local Docker helpers for database and filestore backup and restore.
- Install `restic` in generated Docker images and generate optional Local Docker restic helpers for incremental filestore backup and restore.

## 1.19.10 (2026-09-22)

- Allow `--init-project [ROOT]` to select the workspace root directly.

## 1.19.9 (2026-09-12)

### Changed

- Resolve Docker Python dependencies during image builds instead of workspace generation.

## 1.19.8 (2026-09-11)

### Changed

- Use a pinned official `uv` Docker image in generated Dockerfiles.

## 1.19.7 (2026-09-11)

### Changed

- Regenerate current workspace when `odt-env` is run without arguments.

## 1.19.6 (2026-09-02)

### Documentation

- Update Quick start.

## 1.19.5 (2026-08-31)

### Fixed

- Fix local Docker database configuration.

## 1.19.4 (2026-08-31)

### Fixed

- Fix `--init-project` so workspace artifacts are generated even when no sync, venv, deploy, or bundle options are provided.

## 1.19.3 (2026-08-31)

### Documentation

- Separate Docker and native virtual environment workflows more clearly.

## 1.19.2 (2026-08-31)

### Removed

- Remove `[docker].compose_project_name`, `[docker].db_service` and `[docker].odoo_service` options.

## 1.19.1 (2026-08-28)

### Fixed

- Generate and include `odoo.conf` in the Docker deploy context under `docker/deploy/configs`.

## 1.19.0 (2026-08-27)

### Added

- Add automatic generation of local Docker development artifacts under `docker/local/`.
- Add `--create-docker-deploy` CLI option to generate a self-contained deployment build context under `docker/deploy/`.

### Removed

- Remove the `--build-docker-image` CLI option.
