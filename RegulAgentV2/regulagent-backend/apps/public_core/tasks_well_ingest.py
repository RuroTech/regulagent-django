import logging

from celery import shared_task

from apps.public_core.services.well_ingestor import ingest_nm_wells, ingest_tx_wells

logger = logging.getLogger(__name__)


@shared_task
def ingest_tx_active_wells_task():
    result = ingest_tx_wells(source="active")
    logger.info("ingest_tx_active_wells_task: %s", result)
    return result


@shared_task
def ingest_tx_iwar_wells_task():
    result = ingest_tx_wells(source="iwar")
    logger.info("ingest_tx_iwar_wells_task: %s", result)
    return result


@shared_task
def ingest_nm_active_wells_task():
    result = ingest_nm_wells()
    logger.info("ingest_nm_active_wells_task: %s", result)
    return result
