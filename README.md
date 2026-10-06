# odoo-compose

**Build reproducible Odoo workspaces.**

`odoo-compose`, provided by the `odoo-devops-tools` package, builds and maintains reproducible Odoo workspaces from a single project definition.

A basic workspace definition with addons can look like this:

```ini
[odoo]
version = 19.0

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}
```

From this configuration, `odoo-compose` can generate a workspace such as:

```text
ROOT/
├── docker/                 # generated Docker artifacts
│   ├── local/              # Docker workflow artifacts
│   │   └── scripts/        # database and filestore backup/restore helpers
│   └── deploy/             # self-contained deploy build context
├── odoo/                   # Odoo source
├── odoo-addons/            # addon sources
│   ├── oca-web/
│   └── oca-helpdesk/
├── odoo-configs/           # generated Odoo configuration
├── odoo-scripts/           # run, test, shell, backup, restore, update
├── odoo-data/              # Odoo data directory
├── odoo-logs/              # runtime logs
├── odoo-backups/           # backups created by helper scripts
├── wheelhouse/             # offline Python wheelhouse
├── venv/                   # Python virtual environment
├── compose.yaml            # Docker Compose file
└── odoo-project.ini        # workspace configuration
```

## Key features

* **Reproducible workspaces** — define and recreate an Odoo workspace from a single INI configuration.
* **Docker workflows** — generate local Docker Compose environments with optional [Mailpit](https://mailpit.axllent.org/) mail testing and self-contained deploy build contexts.
* **Portable workflows** — create verified bundles with sources and Python wheels for reproducible or offline use.
* **Python dependency management** — manage Python versions, virtual environments and requirements.
* **Composable configuration** — combine INI layers, variables, and CLI overrides.

`odoo-compose` focuses on creating and maintaining the Odoo workspace and its derived artifacts. Infrastructure provisioning and deployment orchestration remain outside its scope.

---

## System requirements

- **git**: https://git-scm.com/install/
- **uv**: https://docs.astral.sh/uv/getting-started/installation/
- **Docker**: https://docs.docker.com/get-docker/ — optional, for Docker workflows.

---

## Installation

Install the CLI with `uv`:

```bash
uv tool install -U odoo-devops-tools
```

Verify the installation:

```bash
odoo-compose --help
```

---

## Usage

`odoo-compose` supports two development workflows:

- **Docker workflow** — run Odoo and PostgreSQL with the generated Docker Compose environment.
- **Native development with venv** — run Odoo directly on the host using a generated Python virtual environment.

Choose the workflow that fits your environment. Both use the same `odoo-project.ini` project definition.

## 1. Docker workflow

Create a new workspace:

```bash
odoo-compose --init ./odoo19
```

This creates `./odoo19/odoo-project.ini` with this initial content:

```ini
[odoo]
version = 19.0
```

Then start the Docker Compose environment:

```bash
cd ./odoo19
docker compose up -d --build
```

Optionally initialize the Odoo database from the command line instead of through the web interface:

```bash
./docker/local/scripts/run.sh -i base --without-demo=all --stop-after-init
```

Docker Compose runs both Odoo and PostgreSQL. Odoo is available at http://localhost:8069.

### 1.1. Adding extra addons

To extend Odoo with additional functionality, add extra addons through `[addons.<name>]` sections in `odoo-project.ini`.

In this example, we add two Git-based addon repositories, `OCA/web` and `OCA/helpdesk`.

#### 1.1.1. Update the project file

Edit `odoo-project.ini` in the workspace root and add these addon sections:

```ini
[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}
```

The rest of the generated project file can stay unchanged.

#### 1.1.2. Sync and install addons


Sync the configured addon repositories and refresh the generated Docker workflow artifacts:

```bash
odoo-compose --sync-addons
docker compose up -d --build
```

The addon repositories are cloned into `ROOT/odoo-addons/oca-web/` and `ROOT/odoo-addons/oca-helpdesk/` and bind-mounted into the Odoo container.

If an addon source contains a `requirements.txt` file, its Python dependencies are included when the Docker image is rebuilt.

Install the modules from the newly added addon repositories:

```bash
./docker/local/scripts/run.sh -i web_notify,helpdesk_mgmt --without-demo=all --stop-after-init
```

For subsequent addon updates, use the generated Docker update helper:

```bash
./docker/local/scripts/update.sh
```

### 1.2. Running Odoo from workspace source

By default, the generated Docker workflow runs the Odoo provided by `[docker].base_image`.

Use `odoo_source = workspace` when you need to run Odoo from the workspace source.

For example:

```ini
[odoo]
version = 19.0
repo = git@github.com:my-company/odoo.git
branch = 19.0-custom-odoo

[docker]
odoo_source = workspace
```

Sync the Odoo source and generate the Docker artifacts:

```bash
odoo-compose --sync-all
docker compose up -d --build
```

The resolved Odoo source is bind-mounted read-only at `/opt/odoo`. The Odoo Python source from the base image is removed while its installed dependencies remain available.

### 1.3. Mail testing with Mailpit

Enable Mailpit in the local Docker workflow when you need to inspect or test email without sending it to a real SMTP server:

```bash
odoo-compose --set docker:mailpit=true
docker compose up -d --build
```

Mailpit is available at http://localhost:8025 by default. When running multiple workspaces, override the host web UI port, for example:

```bash
odoo-compose --set docker:mailpit=true --set docker:mailpit_webui_port=8026
docker compose up -d --build
```

The generated local Odoo configuration sends outgoing email to `mailpit:1025`. When Mailpit is enabled, local Docker SMTP connection settings (`smtp_server`, `smtp_port`, `smtp_ssl`, `smtp_user`, and `smtp_password`) are intentionally overridden; the deploy configuration keeps the values from `[config]` unchanged.

Mailpit also exposes POP3 inside the Compose network at `mailpit:1110`, using `odoo` / `odoo` credentials. This can be used to test Odoo incoming mail and fetchmail flows by configuring an Odoo Incoming Mail Server with POP3, server `mailpit`, port `1110`, and SSL/TLS disabled.

Mailpit is generated only for the local Docker workflow and is not included in the deploy build context.

### 1.4. Script reference

The Docker workflow generates platform-specific helper scripts under `ROOT/docker/local/scripts/`. Unix-like systems generate `.sh` helpers; Windows generates the corresponding `.bat` helpers.

```text
docker/local/scripts/
├── run.sh
├── shell.sh
├── update.sh
├── backup-db.sh
├── restore-db.sh
├── neutralize-db.sh
├── backup-filestore.sh
├── restore-filestore.sh
├── restic.sh
├── backup-filestore-restic.sh
└── restore-filestore-restic.sh
```

On Unix-like hosts where Docker requires `sudo`, run generated Docker helpers with `ODOO_DOCKER_SUDO=1`.

```bash
ODOO_DOCKER_SUDO=1 ./docker/local/scripts/run.sh --help
```

#### 1.4.1. run

Runs Odoo in a one-off Docker container for the configured database:

```bash
./docker/local/scripts/run.sh
```

Any extra arguments are passed through to Odoo:

```bash
./docker/local/scripts/run.sh -i base --stop-after-init
```

The default database name is taken from `[config].db_name` when configured, otherwise it is `odoo`. Override it with `ODOO_DB_NAME`:

```bash
ODOO_DB_NAME=odoo_test ./docker/local/scripts/run.sh -u sale --stop-after-init
```

#### 1.4.2. shell

Opens an Odoo shell for the configured database:

```bash
./docker/local/scripts/shell.sh
```

The database name follows the same `ODOO_DB_NAME` convention as the `run` helper:

```bash
ODOO_DB_NAME=odoo_test ./docker/local/scripts/shell.sh
```

Any extra arguments are passed through to the underlying Odoo shell command.

#### 1.4.3. update

Updates installed addons using `click-odoo-update`:

```bash
./docker/local/scripts/update.sh
```

The database name follows the same `ODOO_DB_NAME` convention as the `run`, shell, and database helpers.

Any extra arguments are passed through to `click-odoo-update`:

```bash
./docker/local/scripts/update.sh --update-all
```

#### 1.4.4. Database backup

Create a PostgreSQL custom-format dump under `ROOT/odoo-backups/`:

```bash
./docker/local/scripts/backup-db.sh
```

The default output name is timestamped, for example:

```text
odoo-backups/odoo_20260923_093000.dump
```

Pass an output path explicitly when needed:

```bash
./docker/local/scripts/backup-db.sh ./odoo-backups/pre-upgrade.dump
```

The default database name is taken from `[config].db_name` when configured, otherwise it is `odoo`. Override it for any command with `ODOO_DB_NAME`:

```bash
ODOO_DB_NAME=odoo_test ./docker/local/scripts/backup-db.sh
```

#### 1.4.5. Database restore

Restore a database dump with `pg_restore`:

```bash
./docker/local/scripts/restore-db.sh ./odoo-backups/pre-upgrade.dump
```

Database restores use copy semantics by default, matching `click-odoo-restoredb --copy`.
After `pg_restore`, Odoo database identity parameters are regenerated so the restored
database can coexist with the source database:

```bash
./docker/local/scripts/restore-db.sh --copy ./odoo-backups/pre-upgrade.dump
```

Use `--move` only when the restored database replaces the original database and its
identity must be preserved:

```bash
./docker/local/scripts/restore-db.sh --move ./odoo-backups/pre-upgrade.dump
```

If the target database already exists, the restore is rejected unless `--force` is used. With `--force`, the existing database is dropped and recreated before the dump is restored. When restoring the database currently used by the Odoo service, stop Odoo first and start it again after the restore:

```bash
docker compose stop odoo
./docker/local/scripts/restore-db.sh --force ./odoo-backups/pre-upgrade.dump
docker compose up -d odoo
```

To restore the same dump into another database without replacing the default database:

```bash
ODOO_DB_NAME=odoo_restore ./docker/local/scripts/restore-db.sh ./odoo-backups/pre-upgrade.dump
```

#### 1.4.6. Database neutralization

For development or test databases restored from production, run the generated neutralization helper explicitly:

```bash
./docker/local/scripts/neutralize-db.sh
```

Override the target database with `ODOO_DB_NAME` when needed:

```bash
ODOO_DB_NAME=odoo_test ./docker/local/scripts/neutralize-db.sh
```

For Odoo 16.0 and newer, the helper uses Odoo's native database neutralization mechanism. This applies the `neutralize.sql` scripts provided by installed Odoo modules, disabling or redirecting production-side effects such as scheduled actions, outgoing email, and supported external integrations.

For Odoo 13.0 through 15.0, where the native neutralization framework is not available, the helper applies a deliberately minimal compatibility neutralization through the Odoo ORM: it disables scheduled actions, configured outgoing mail servers, and configured incoming mail servers.

Neutralization is intentionally separate from database restore. A typical test/staging restore workflow is therefore:

```bash
docker compose stop odoo
./docker/local/scripts/restore-db.sh --copy ./odoo-backups/production.dump
./docker/local/scripts/restore-filestore.sh ./odoo-backups/production-filestore.tar.gz
./docker/local/scripts/neutralize-db.sh
docker compose up -d odoo
```

`--copy` / `--move` and neutralization serve different purposes: copy/move controls restored database identity, while neutralization controls production side effects.

#### 1.4.7. Filestore backup

Create a compressed tar archive of the selected database filestore:

```bash
./docker/local/scripts/backup-filestore.sh
```

The default output name is timestamped, for example:

```text
odoo-backups/odoo_20260923_093000_filestore.tar.gz
```

Pass an output path explicitly when needed:

```bash
./docker/local/scripts/backup-filestore.sh ./odoo-backups/pre-upgrade-filestore.tar.gz
```

The helper uses a one-off Odoo container with the same `odoo-data` volume, so the main Odoo service does not need to be running.

When creating a matching database and filestore backup for migration or disaster recovery, stop Odoo first to prevent writes while both parts are captured:

```bash
docker compose stop odoo
./docker/local/scripts/backup-db.sh ./odoo-backups/pre-upgrade.dump
./docker/local/scripts/backup-filestore.sh ./odoo-backups/pre-upgrade-filestore.tar.gz
docker compose up -d odoo
```

#### 1.4.8. Filestore restore

Restore a filestore archive:

```bash
./docker/local/scripts/restore-filestore.sh ./odoo-backups/pre-upgrade-filestore.tar.gz
```

If the target filestore already exists, the restore is rejected unless `--force` is used. With `--force`, the existing filestore directory is removed before the archive is extracted. When restoring the filestore currently used by the Odoo service, stop Odoo first:

```bash
docker compose stop odoo
./docker/local/scripts/restore-filestore.sh --force ./odoo-backups/pre-upgrade-filestore.tar.gz
docker compose up -d odoo
```

A filestore can also be restored under another database name:

```bash
ODOO_DB_NAME=odoo_restore ./docker/local/scripts/restore-filestore.sh ./odoo-backups/pre-upgrade-filestore.tar.gz
```

Database and filestore backups are intentionally separate. This allows either part to be restored independently while still making it possible to create matching database and filestore backups when both are needed.

#### 1.4.9. Restic filestore backup

The generated Docker image also includes `restic`. Restic is an additional option intended especially for large filestores where incremental snapshots and deduplication are useful.

Set `RESTIC_REPOSITORY` and either `RESTIC_PASSWORD_FILE` or `RESTIC_PASSWORD` before using the helpers. For a local repository on the Docker host, use an absolute path. The wrapper automatically bind-mounts the repository into the one-off container; Windows drive paths such as `C:\backups\odoo` are supported by `restic.bat`:

```bash
export RESTIC_REPOSITORY=/srv/restic/odoo
export RESTIC_PASSWORD_FILE=/etc/restic/odoo-password

./docker/local/scripts/restic.sh init
```

A remote restic repository URL can be used instead of a local path. Backend-specific authentication or external helper programs, when required by that backend, must also be made available to the container.

Create a restic snapshot of the selected database filestore:

```bash
./docker/local/scripts/backup-filestore-restic.sh
```

The default database name follows the same `ODOO_DB_NAME` convention as the archive helpers. Snapshots are tagged with `odoo`, `filestore`, and `db:<database>`. The helper also uses a stable host identifier from the Docker host for restic snapshot grouping; override it with `RESTIC_HOST` when needed.

An optional backup identifier can be added as another snapshot tag:

```bash
RESTIC_BACKUP_ID=20260923_020000 ./docker/local/scripts/backup-filestore-restic.sh
```

This is useful for pairing a PostgreSQL dump with the corresponding filestore snapshot.

List or inspect snapshots through the generic wrapper:

```bash
./docker/local/scripts/restic.sh snapshots
./docker/local/scripts/restic.sh check
```

#### 1.4.10. Restic filestore restore

Restore the latest matching filestore snapshot:

```bash
docker compose stop odoo
./docker/local/scripts/restore-filestore-restic.sh --force
docker compose up -d odoo
```

If the target filestore does not exist yet, `--force` is not required. If it already exists, the restore is rejected unless `--force` is used.

Or restore a specific snapshot ID:

```bash
docker compose stop odoo
./docker/local/scripts/restore-filestore-restic.sh --force SNAPSHOT_ID
docker compose up -d odoo
```

To restore a filestore backed up under one database name into another database, set the target with `ODOO_DB_NAME` and the source snapshot path with `RESTIC_SOURCE_DB_NAME`:

```bash
ODOO_DB_NAME=odoo_restore RESTIC_SOURCE_DB_NAME=odoo ./docker/local/scripts/restore-filestore-restic.sh SNAPSHOT_ID
```

With `--force`, the restore helper removes the target database filestore before restoring it. When `latest` is used, the snapshot selection is restricted to the current `RESTIC_HOST` and source filestore path. Use the same `RESTIC_HOST` value that was used when the snapshot was created if the restore is performed from another host.

### 1.5. Creating a Docker deploy build context

Use `--create-docker-deploy` to generate a self-contained Docker build context for CI/CD, testing, staging, production, or another non-local deployment workflow.

Addon modules are staged into the build context so the resulting image does not depend on bind-mounted workspace sources:

```bash
odoo-compose --sync-addons --create-docker-deploy
```

When `[docker].odoo_source = workspace`, sync the Odoo source as well:

```bash
odoo-compose --sync-all --create-docker-deploy
```

This additionally creates:

```text
ROOT/docker/deploy/
├── Dockerfile
├── .dockerignore
├── addons/
├── odoo/                    # only with odoo_source = workspace
├── requirements/
└── configs/
    └── odoo.conf
```

Addon modules are staged under `docker/deploy/addons/` and copied into `/mnt/extra-addons/` by the generated Dockerfile.

With `odoo_source = workspace`, the resolved Odoo source is also staged under `docker/deploy/odoo/` and copied into `/opt/odoo/`, so the deploy image is self-contained.

---

## 2. Native development with venv

In this workflow, Odoo runs directly on the host using a generated Python virtual environment.

Create the workspace with the Odoo source and virtual environment:

```bash
odoo-compose --init ./odoo19 --sync-all --create-venv \
  --set config:db_host=127.0.0.1 \
  --set config:db_name=odoo \
  --set config:db_user=odoo \
  --set config:db_password=odoo
```

> **Note**
> Make sure PostgreSQL is running at the configured host and the configured database user exists.

This creates `./odoo19/odoo-project.ini` with this initial content:

```ini
[odoo]
version = 19.0

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

Then start Odoo with the generated script:

```bash
cd ./odoo19
./odoo-scripts/run.sh
```

Odoo starts with the generated configuration from `./odoo-configs/odoo-server.conf` and is available at http://localhost:8069.

### 2.1. Adding extra addons

To extend Odoo with additional functionality, add extra addons through `[addons.<name>]` sections in `odoo-project.ini`.

In this example, we add two Git-based addon repositories, `OCA/web` and `OCA/helpdesk`.

#### 2.1.1. Update the project file

Edit `odoo-project.ini` in the workspace root and add these addon sections:

```ini
[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}
```

The rest of the generated project file can stay unchanged.

#### 2.1.2. Sync and install addons

Sync the sources and recreate the Python environment so dependencies from the new addon repositories are included:

```bash
odoo-compose --sync-all --create-venv
```

The addon repositories are cloned into `ROOT/odoo-addons/oca-web/` and `ROOT/odoo-addons/oca-helpdesk/`, and their directories are added to the generated `addons_path`.

Start Odoo and install the modules from the newly added addon repositories:

```bash
./odoo-scripts/run.sh -i web_notify,helpdesk_mgmt
```

For subsequent addon updates, use the generated update script:

```bash
./odoo-scripts/update.sh
```

The generated script uses `click-odoo-update` with the workspace Odoo configuration.

### 2.2. Using system Python instead of managed Python

By default, `odoo-compose` uses `uv` to install and manage the requested Python version.

If you already have a suitable system Python installed, you can disable managed Python.

#### 2.2.1. Update the project file

Disable managed Python by adding `python_version = 3.11` and `managed_python = false` to the `odoo-project.ini` file.

> **Note**
> Set `python_version` to the Python version you want to use from your local system.
> In the example below, 3.11 is only illustrative.

```ini
[virtualenv]
managed_python = false
python_version = 3.11
```

#### 2.2.2. Update the workspace

After changing the project file, run `odoo-compose` again from the workspace root:

```bash
odoo-compose --sync-all --create-venv
```

This recreates the virtual environment at `ROOT/venv` using the system Python.

### 2.3. Script reference

This section describes the native helpers under `ROOT/odoo-scripts/`. Docker backup/restore helpers are documented in the Docker workflow section above.

Most helper scripts are generated in both Unix (`.sh`) and Windows (`.bat`) variants. `instance.sh` is available only on Unix-like systems.

Native database backup and restore scripts are generated only when `[config].db_name` is configured.

The examples below use the Unix form.

#### 2.3.1. run

Starts Odoo in the foreground.

Any extra arguments are forwarded to the underlying command `odoo-bin`.

Examples:

```bash
./odoo-scripts/run.sh
./odoo-scripts/run.sh --dev=all
./odoo-scripts/run.sh -i sale,crm --without-demo=all
```

#### 2.3.2. instance

Manages Odoo as a background service on Unix-like systems.

Logs are written to `ROOT/odoo-logs/odoo-server.log` and the PID is stored in `ROOT/odoo-logs/odoo-server.pid`.

Examples:

```bash
./odoo-scripts/instance.sh start
./odoo-scripts/instance.sh stop
./odoo-scripts/instance.sh restart
./odoo-scripts/instance.sh status
```

#### 2.3.3. test

Runs Odoo tests.

The script always adds `--test-enable --stop-after-init`.

Any extra arguments are forwarded to the underlying command `odoo-bin`.

Examples:

```bash
./odoo-scripts/test.sh
./odoo-scripts/test.sh -i sale --test-tags /sale
```

#### 2.3.4. shell

Opens an Odoo shell.

Examples:

```bash
./odoo-scripts/shell.sh
```

#### 2.3.5. backup

Creates a timestamped ZIP backup under `ROOT/odoo-backups/`.

Any extra arguments are forwarded to the underlying command `click-odoo-backupdb` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-backupdb-beta) package.

Examples:

```bash
./odoo-scripts/backup.sh
```

#### 2.3.6. restore

Restores a backup into the configured database.

The script always adds `--copy --neutralize`.

Any extra arguments are forwarded to the underlying command `click-odoo-restoredb` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-restoredb-beta) package.

Examples:

```bash
./odoo-scripts/restore.sh ./odoo-backups/odoo_20260331_221443.zip
./odoo-scripts/restore.sh ./odoo-backups/odoo_20260331_221443.zip --force
```

#### 2.3.7. update

Updates an Odoo database automatically detecting addons to update based on a hash of their file content.

Any extra arguments are forwarded to the underlying command `click-odoo-update` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-update-stable) package.

Examples:

```bash
./odoo-scripts/update.sh
./odoo-scripts/update.sh --update-all
```

---

## 3. Managing Python requirements

The `[virtualenv]` section controls additional Python dependencies used when provisioning both the native virtual environment and generated Docker images.

Use it to add new packages, pin specific versions, and override packages collected from Odoo or addon repository `requirements.txt` files.

Use:

- `requirements` to add extra packages or pin an explicit version
- `constraints` to restrict dependency versions without installing packages by themselves
- `build_constraints` to restrict build-time dependency versions
- `requirements_ignore` to skip packages that would otherwise be collected from repository requirements files

When a package is listed in `requirements`, `odoo-compose` automatically gives that package priority over the same package name from collected repository requirements and `constraints`. This means you can usually pin a package version just by adding it to `requirements`.

### 3.1. Add or pin packages

Use `requirements` to install additional packages or to force a specific version:

```ini
[virtualenv]
requirements =
  requests==2.32.3
  boto3==1.35.99
```

In this example, both packages are included in the generated dependency set and pinned to the specified versions.

### 3.2. Constrain dependency versions

Use `constraints` to restrict versions selected by the dependency resolver without adding those packages to the installation set:

```ini
[virtualenv]
constraints =
  urllib3<2
  lxml<6
```

A constraint only applies when the package is required by another dependency. If the same package is listed explicitly in `requirements`, the explicit requirement takes priority.

### 3.3. Override a package with a different one

If you want to replace a package with a different distribution name, add the replacement to `requirements` and skip the original package with `requirements_ignore`.

Example:

```ini
[virtualenv]
requirements =
  psycopg2-binary==2.9.9
requirements_ignore =
  psycopg2
```

In this example, `odoo-compose` installs `psycopg2-binary==2.9.9` and skips `psycopg2` when collecting repository requirements.

---

## 4. Creating portable workspace bundles

Portable bundles are useful when you want to prepare an Odoo workspace on an internet-connected machine and reproduce it on another compatible machine without cloning repositories or downloading Python packages again.

A portable workspace bundle is a ZIP archive containing:

```text
odoo/
odoo-addons/
wheelhouse/
manifest.json
odoo-project.ini
```

The bundle does not contain the virtual environment, database data, logs, backups, generated scripts, generated configuration files, Docker artifacts, Git metadata, or provisioning history.

Those machine-specific outputs are recreated on the target machine.

### 4.1. Create a bundle on the build machine

On an internet-connected build machine, sync the sources, build the wheelhouse, and create the bundle in one command:

```bash
odoo-compose --sync-all --create-venv --create-bundle
```

When no output path is supplied, the bundle is written to:

```text
ROOT/dist/ROOT-NAME.odt.zip
```

You can also select an explicit output file:

```bash
odoo-compose --sync-all --create-venv --create-bundle ./artifacts/odoo18-production.odt.zip
```

#### 4.1.1. Including uncommitted changes

By default, bundle creation stops when a bundled Git repository has uncommitted changes.

To intentionally include those changes in the bundle, use:

```bash
odoo-compose --sync-all --create-venv --create-bundle --allow-dirty-bundle
```

### 4.2. Create the workspace on the target machine

Copy the ZIP to the target machine and import it into an empty directory:

```bash
odoo-compose --create-from-bundle ./odoo18-production.odt.zip \
  --root ./odoo18-prod \
  --set config:db_host=127.0.0.1 \
  --set config:db_name=odoo \
  --set config:db_user=odoo \
  --set config:db_password=odoo
```

The import operation:

1. verifies the bundle format, platform, and CPU architecture;
2. rejects unsafe ZIP paths, duplicate entries, and symbolic links;
3. verifies every bundled file using its size and SHA-256 checksum;
4. extracts Odoo, addons, the sanitized project INI, and the wheelhouse;
5. recreates `ROOT/venv` strictly from the bundled wheelhouse;
6. regenerates configuration files and helper scripts.

`ROOT` must be empty. If `--root` is omitted, the current working directory is used and must be empty. The imported manifest is saved as `ROOT/.odt-env/imported-bundle-manifest.json`.

> **Compatibility note**
> A wheelhouse is platform- and architecture-dependent. Create and import a bundle on compatible systems, for example Linux x86-64 to Linux x86-64. The target machine must have `uv` and access to the configured Python version. When `[virtualenv].managed_python = true`, `uv` may still need network access if that Python interpreter is not already installed or cached. For a fully disconnected target, install the required Python interpreter beforehand or use `managed_python = false`.

### 4.3. Manual wheelhouse workflow

The existing manual workflow remains available. After preparing a complete workspace on the build machine, copy the whole workspace and run this command from the copied root:

```bash
odoo-compose --create-venv-from-wheelhouse --no-local-docker
```

This recreates `ROOT/venv`, skips dependency compilation and wheelhouse building, and installs strictly from the existing `ROOT/wheelhouse/` and `all-requirements.lock.txt`.

---

## Command-line reference

### Syntax

```text
odoo-compose [INI] [OPTIONS]
```

If no arguments are specified, `odoo-compose` treats the current working directory as ROOT and, when `ROOT/odoo-project.ini` exists,
regenerates the workspace artifacts without syncing repositories or recreating the virtual environment.

### Project definition (`INI`)

`INI` is the optional project definition source used by `odoo-compose`. It can be:

  - a local filesystem path, for example:

    ```bash
    odoo-compose /path/to/odoo-project.ini
    ```

  - a remote INI loaded from a Git repository, for example:

    ```bash
    odoo-compose 'git+https://github.com/lck/odoo-devops-tools.git//examples/odoo-project.ini?ref=main'
    odoo-compose 'git+git@github.com:company/repo.git//examples/odoo-project.ini?ref=main'
    ```

    Syntax:

    ```text
    git+REPO_URL//PATH/TO/PROJECT.ini?ref=REF
    ```

  - a remote INI loaded from a URL, for example:

    ```bash
    odoo-compose 'https://github.com/lck/odoo-devops-tools/blob/main/examples/odoo-project.ini'
    odoo-compose 'https://raw.githubusercontent.com/lck/odoo-devops-tools/main/examples/odoo-project.ini'
    ```

#### Default project file convention

If no positional `INI` file is provided and no `-i/--include` option is used, `odoo-compose` looks for `ROOT/odoo-project.ini`.

This is similar to how Docker Compose uses `compose.yaml` by convention.

For example, this command:

```bash
odoo-compose --root ./existing-workspace --sync-all --create-venv
```

is equivalent to passing the default project file explicitly:

```bash
odoo-compose ./existing-workspace/odoo-project.ini --sync-all --create-venv
```

#### INI includes

Use `-i INI` / `--include INI` to include additional project layers. The option can be repeated.

```bash
odoo-compose base-odoo-project.ini -i local-overrides.ini -i extra-addons.ini --sync-all --create-venv
```

Project layers are processed from left to right. Later layers override earlier layers.

Validation is performed only after all layers have been merged.

The merged project file is saved as `ROOT/odoo-project.ini`, replacing any existing file at that path.

### Paths and outputs

- `--root ROOT` — workspace root directory. Default: the directory containing a local INI file, or the current working directory for a remote INI or omitted INI. In include mode, the default is the directory of the first local source, or the current working directory when the first source is remote.
- `--init [ROOT]` — create `ROOT/odoo-project.ini` from the bundled default template if it does not already exist. `ROOT` is optional; when supplied, it is a shorthand for selecting the workspace root directly, for example `odoo-compose --init ./odoo19`. When the optional value is omitted, `--root ROOT` remains supported for backward compatibility. Do not supply both `--init ROOT` and `--root ROOT`; the command exits with an error instead of choosing one implicitly. This option is valid only when `INI` is omitted and no `-i/--include` is provided. Existing project files are not overwritten.
- `--include INI`, `-i INI` — include an additional project INI layer; can be repeated. Later layers override earlier layers.
- `--extra-var KEY=VALUE`, `-e KEY=VALUE` — override or inject a value in the optional `[vars]` section; can be repeated.
- `--set SECTION:KEY=VALUE`, `-S SECTION:KEY=VALUE` — set or override a supported project option; can be repeated. Missing supported sections/options are created automatically. Structured sections (`[virtualenv]`, `[odoo]`, `[addons.<name>]`, and `[docker]`) accept only documented keys; `[config]` remains open to standard Odoo configuration options except `addons_path`.
- `--no-configs` — do not generate config files.
- `--no-scripts` — do not generate helper scripts under `ROOT/odoo-scripts/`.
- `--no-data-dir` — do not create the Odoo data directory.
- `--no-provisioning-log` — do not write provisioning metadata under `ROOT/.odt-env/`.
- `--show-last-run` — print metadata from `ROOT/.odt-env/last-provisioning.json` and exit without provisioning.

### Repository sync

- `--sync-odoo` — sync only the Git-managed Odoo source; when `[odoo].path` is used, the local path is reused and Git sync is skipped.
- `--sync-addons` — sync only `ROOT/odoo-addons/*`.
- `--sync-all` — sync both Odoo and addons.

> **Note**
> If any target repository contains local uncommitted changes, `odoo-compose` aborts the sync operation.
> Commit, stash, or discard the changes before running a sync command.

### Python, virtual environment, and wheelhouse

Online virtual environment provisioning:

- `--create-venv` — recreate `ROOT/venv` and refresh the wheelhouse; if `ROOT/venv` already exists, it is deleted and created again.

Offline deployment from a prebuilt wheelhouse:

- `--create-venv-from-wheelhouse` — recreate `ROOT/venv` from an existing `ROOT/wheelhouse/` and `all-requirements.lock.txt`, install strictly offline, and skip lock compilation and wheelhouse build. This is useful after preparing dependencies on an internet-connected build machine and copying the workspace to a target machine without internet access.

Maintenance:

- `--clear-pip-wheel-cache` — remove all items from pip's wheel cache.

### Portable workspace bundles

- `--create-bundle [BUNDLE]` — create a verified portable ZIP containing Odoo sources, configured addon sources, a sanitized `odoo-project.ini`, and `ROOT/wheelhouse/`. If `BUNDLE` is omitted, the output is `ROOT/dist/ROOT-NAME.odt.zip`. Relative explicit output paths are resolved from the current working directory.
- `--allow-dirty-bundle` — allow `--create-bundle` to snapshot Git repositories with uncommitted changes. Without this option, dirty repositories abort bundle creation.
- `--create-from-bundle BUNDLE` — verify and extract a portable bundle into an empty `ROOT`, then recreate `ROOT/venv` using the bundled wheelhouse. This offline deployment path intentionally skips Docker workflow generation; `--create-docker-deploy` cannot be combined with it.

### Docker generation

- Docker workflow generation is enabled by default. It regenerates `ROOT/docker/local/` and `ROOT/compose.yaml`; addon sources are bind-mounted from the workspace into the Odoo container. With `[docker].odoo_source = workspace`, the resolved Odoo source is also bind-mounted at `/opt/odoo` and used as the runtime Odoo source. Set `[docker].mailpit = true` to add a persistent Mailpit service for local SMTP and POP3 testing. Database and filestore backup/restore helpers, including optional restic filestore helpers, are generated for the current platform under `ROOT/docker/local/scripts/` (`.sh` on Unix-like systems, `.bat` on Windows).
- `--no-local-docker` — skip regeneration of `ROOT/docker/local/` and `ROOT/compose.yaml`. Existing files are not deleted.
- `--create-docker-deploy` — generate a self-contained deployment build context under `ROOT/docker/deploy/`. Addon modules are staged into the context; with `[docker].odoo_source = workspace`, the resolved Odoo source is staged as well.

### Other options

- `--version` — show the installed `odoo-compose` version and exit.

---

## Project file reference

The `odoo-compose` project file is an INI file that describes the Odoo workspace to create.

At minimum, the project file must contain this section:

- `[odoo]`

The following sections are supported:

- `[vars]` — optional reusable variables for INI interpolation
- `[virtualenv]` — optional Python and dependency settings
- `[odoo]` — required Odoo source settings
- `[addons.<name>]` — optional addon sources
- `[docker]` — optional Docker workflow/deploy generation settings
- `[config]` — optional Odoo server configuration values

### General rules

- The project file can have any filename when passed explicitly. When `INI` is omitted, `odoo-compose` uses the existing `ROOT/odoo-project.ini`; if it is missing, use `--init` to create it explicitly from the bundled default template. Remote INI sources and merged include layers are materialized as `ROOT/odoo-project.ini`.
- INI interpolation is supported, so values such as `${odoo:version}` can be reused across sections.
- Multiple INI layers can be composed with `-i/--include`. Later layers override earlier layers; multi-line values are replaced as whole option values, not appended.
- The optional `[vars]` section is useful for reusable values referenced as `${vars:name}`.
- Values from `[vars]` can be overridden or injected from the CLI with `-e name=value` / `--extra-var name=value`.
- Supported project options can be set or overridden directly with `-S section:key=value` / `--set section:key=value`, even when the section or option is omitted from the INI file. Structured sections accept only their documented keys.
- Multi-line values are used for lists such as `requirements`, `constraints`, `build_constraints`, and `requirements_ignore`.

### `[vars]`

This section is optional.

Use it for reusable values that you want to interpolate in other sections.

A major advantage of `[vars]` is that its values can also be overridden directly from the CLI with `-e KEY=VALUE` / `--extra-var KEY=VALUE`. This makes it easy to keep a single project file and adjust things like Odoo version, branch, commit, or database name per run without editing the file.

Example:

```ini
[vars]
branch = 18.0
db = odoo

[odoo]
version = 18.0
branch = ${vars:branch}

[config]
db_name = ${vars:db}
db_user = odoo
db_password = odoo
```

CLI override example:

```bash
odoo-compose odoo-project.ini --sync-all --create-venv -e branch=dev -e db=odoo_dev
```

### `[virtualenv]`

This section is optional.

- `python_version` — Python version for the virtual environment. If omitted, `odoo-compose` chooses a default version based on the selected Odoo version.
- `managed_python` — whether `uv` should install and manage Python automatically. Default: `true`.
- `requirements` — additional Python requirements to install. Multi-line list.
- `constraints` — dependency constraints used during resolution without installing packages by themselves. Explicit `requirements` take priority over matching constraints. Multi-line list.
- `build_constraints` — additional build constraints used during dependency compilation. Multi-line list.
- `requirements_ignore` — package names to ignore when collecting requirements from addon repositories. Multi-line list.

Example:

```ini
[virtualenv]
managed_python = false
python_version = 3.11
requirements =
  lxml>=6
  psycopg2-binary==2.9.9
constraints =
  urllib3<2
requirements_ignore =
  psycopg2
```

### `[odoo]`

This section is required.

- `version` — Odoo version in `X.0` format, for example `18.0`. Required.
- `path` — local Odoo source directory. Relative paths are resolved relative to `ROOT/`.
- `repo` — Git repository URL for Odoo. Default: the official Odoo repository.
- `branch` — Git branch to check out. Default: the same value as `version`.
- `commit` — optional Git commit to check out after fetching the selected branch. When set, the repository is pinned to that exact revision.
- `shallow` — whether to use a shallow clone. Default: `true`. Ignored when `commit` is set.

Odoo must use exactly one of these source modes:

- local Odoo source: `version` + `path`
- Git-managed Odoo source: `version` + optional `repo`, `branch`, `commit`, and `shallow`

Git-managed example:

```ini
[odoo]
version = 18.0
repo = https://github.com/odoo/odoo.git
branch = 18.0
commit = e6ec487
shallow = true
```

Local source example:

```ini
[odoo]
version = 18.0
path = ../odoo
```

### `[addons.<name>]`

Addon sections are optional. You can define as many as needed.

Each addon must use exactly one of these source types:

- local addon path: `path`
- Git repository: `repo` + `branch` (+ optional `commit` and `shallow`)

Rules:

- For a local addon, use only `path`.
- For a Git addon, `repo` and `branch` are required.
- `commit` is optional for a Git addon. When set, the repository is pinned to that exact revision.
- `shallow` is optional for Git addons and defaults to `true`. It is ignored when `commit` is set.
- Relative local paths are resolved relative to `ROOT/`.
- Git-based addons are cloned into `ROOT/odoo-addons/<name>/`.
- All configured addon directories are automatically appended to the generated `addons_path`.

Examples:

```ini
[addons.my-custom-addons]
path = odoo-addons/my-custom-addons

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}
commit = abcdef1
```

### `[docker]`

This section is optional.

- `base_image` — Docker image used as the base image in generated Dockerfiles. Default: `odoo:${odoo:version}`.
- `odoo_source` — selects the Odoo source for Docker. `image` uses Odoo from `base_image`; `workspace` uses the workspace Odoo source. Default: `image`.
- `mailpit` — enables a persistent Mailpit service in the local Docker workflow for SMTP and POP3 mail testing. Default: `false`. It does not affect the deploy build context.
- `mailpit_webui_port` — host port used for the Mailpit web UI. Default: `8025`. The internal Mailpit web UI port remains `8025`.

### `[config]`

This section is optional.

When present, it contains Odoo server configuration values written into `ROOT/odoo-configs/odoo-server.conf`.

When omitted, `odoo-compose` still generates a valid config file with generated values such as `addons_path` and `data_dir`.

You can define standard Odoo configuration options here.

Special rules:

- `addons_path` must not be set in `[config]`. `odoo-compose` always generates it automatically.
- `data_dir` may be set in `[config]`. If provided, it overrides the default data directory location.

Example:

```ini
[config]
db_host = 127.0.0.1
db_port = 5432
db_name = odoo
db_user = odoo
db_password = odoo
http_port = 8069
```
