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
