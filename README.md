# MySQL & MariaDB

Connect MySQL and MariaDB data to Flow Steward through direct MySQL, MariaDB or Cloud SQL for MySQL profiles. Read-only Datasets support testing the connection, inspecting table schemas, validating saved SQL against selected tables and columns, previewing results, executing saved queries and inferring output schemas. Use application lookups, reporting extracts and reusable queries to supply data to later workflow steps.

A separate workflow action executes one INSERT, UPDATE or DELETE with named parameters and an optional parameter schema. Use it to save workflow results or change application records while keeping Dataset queries read-only. Parameter values are supplied separately from the SQL statement, allowing the same query or write to be reused with different workflow inputs.

Direct MySQL and MariaDB connections need a host, database, user and password. The Cloud SQL profile uses project, region, instance and database settings with the appropriate Google Cloud access and authentication configuration. Read output is limited to 100 rows and 480 KiB. SQL support is a restricted subset; schema-changing statements, transaction scripts and database triggers are outside the connector's operation set.

## Connection and operation reference

Supports profiles: `mysql`, `mariadb`, `cloud-sql-mysql`. Read-only datasets support schema inspection, validation, bounded previews and saved-query execution. Workflows support one bound INSERT, UPDATE or DELETE, with project connection references and durable effect claims. No triggers or DDL are exposed.

Direct MySQL and MariaDB connections use host, port, database and user, with password in project secrets. Cloud SQL for MySQL uses Google project, region, instance and IP-type settings. Its managed authentication can use inherited application-default credentials, with optional database user/password settings. The pinned definitions under `scripts/upstream-prebuilts/` document exact environment names and defaults.

SQL is a deliberately strict subset: raw backslashes and dollar characters are rejected before parsing; pass those characters through bound parameter values. Reads return at most 100 rows and 480 KiB. Writes have no automatic retry; an uncertain result is reported as unknown. Test mode suppresses writes before driver invocation.

The bundled AMD64 and ARM64 native executables are unchanged, checksum-verified minimal builds from the prior database-specific package. `toolbox-provenance.json`, `SHA256SUMS` and `THIRD_PARTY.yml` retain the pinned upstream source, original binding patch and build image. No other database adapter is installed. The common engine exists only as a pinned build dependency; `scripts/build_toolbox.sh` accepts no database selector and compiles only this package’s profiles.

Build: `scripts/build_toolbox.sh`. Package: `python3 scripts/package_extension.py --runtime-target linux-amd64` and `linux-arm64`. Development tests run through Flow Steward’s Docker extension gate. No live cloud account verification is claimed.

Credential validation: Password is required for mysql, mariadb. Supply password or the matching profile-specific secret env variable. Managed PostgreSQL/MySQL/AlloyDB IAM profiles keep password optional; public config env is not a credential source. Required profiles reject missing/blank secrets before provider dispatch, including workflow test mode. The secret environment alternative belongs in project secrets, for example `env: {PROFILE_PASSWORD: ...}` using the exact variable from the selected pinned profile. Wrong-profile environment keys do not satisfy the requirement.

## Automated CI and releases

Pushes to `main` and pull requests run the shared extension build workflow. The checked-in `.github/release-config.json` selects both Linux targets, the repository's package script and test inputs. Missing native payloads and published-only inventory are restored from checksum-pinned release archives; tracked source files are preserved. The source packager retains its provenance checks.

After the versioned release commit is on `main`, publish by pushing its version tag:

```sh
git tag v1.3.7
git push origin v1.3.7
```

The release workflow runs the shared checks, builds the target archives and publishes the GitHub release and checksums. Do not move an existing tag. To rerun a release for an existing tag, dispatch the same workflow:

```sh
gh workflow run release.yml --repo Flow-Steward/ext-data-mysql --ref main -f tag=v1.3.7
```

The manifest, descriptor (when present), package version and tag must identify the same release. Release publication uses the workflow token; no additional repository secret is required by these callers.

### Standalone CI coverage

The release recipe lists the standalone test files and individual test nodes explicitly, including package, source and provenance contracts. The shared runner clears repository pytest addopts so packaged contract checks are not silently deselected. This scope does not claim live provider or database verification.

The selected contract suite uses mocked provider responses and does not require a provider account.

## Workflow action setup and examples

See [Workflow actions](docs/workflow-actions.md) for choosing Dataset reads versus explicit writes, connection requirements and copyable field examples.
