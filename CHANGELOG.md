# Changelog

## [Unreleased]

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
