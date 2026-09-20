from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from flask import current_app

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
logger = logging.getLogger("alembic.env")


def get_engine():
    return current_app.extensions["migrate"].db.engine


def get_metadata():
    import smartdesk.models  # noqa: F401  (registers all tables)

    return current_app.extensions["migrate"].db.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(get_engine().url).replace("%", "%%"),
        target_metadata=get_metadata(),
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=get_metadata())
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
