"""Running the migrations in-process must not silence the application loggers."""

import logging

from app.services import checker, keycache


def test_app_loggers_stay_enabled_after_the_migration(_migrated_database):
    assert not checker.logger.disabled
    assert not keycache.logger.disabled
    assert not logging.getLogger("quotaguard.webhooks").disabled
