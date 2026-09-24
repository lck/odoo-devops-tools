# Changelog

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
