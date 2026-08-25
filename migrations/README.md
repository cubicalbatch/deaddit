# Migrations (Alembic via Flask-Migrate)

Schema is owned by Alembic. The app no longer calls `db.create_all()`;
`flask init-db` applies migrations and seeds default settings.

All commands use the Flask CLI entrypoint:

```
uv run flask --app deaddit.wsgi <command>
```

## Revisions

| Revision      | Message                                        |
|---------------|------------------------------------------------|
| `359878740bb0`| baseline schema (all 9 tables, FKs, uniques)   |
| `5b2dab0b6816`| composite indexes for feed and job queries     |

The baseline revision reflects the schema as it existed before A3 plus
nothing new; the composite-index revision layers on top of it. Model-level
`__table_args__` declarations match migration head, so future autogenerate
runs stay clean.

## Fresh database (empty file, first boot)

```
uv run flask --app deaddit.wsgi db upgrade
```

Creates all tables from scratch at head. `flask init-db` does the same and
additionally seeds default settings.

## Existing pre-A3 database

The live DB already has the full schema but no `alembic_version` table.
Stamp it at the baseline revision, then upgrade to head:

```
uv run flask --app deaddit.wsgi db stamp 359878740bb0
uv run flask --app deaddit.wsgi db upgrade
```

The upgrade only adds the four composite indexes; data is untouched.

## Rollback

There is no down-migration path in production. Rollback = restore the
pre-migration copy of `instance/deaddit.db` taken before running the
commands above (e.g. `deaddit.db.pre-a3-*`).
