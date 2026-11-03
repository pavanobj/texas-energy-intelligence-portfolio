"""Local, testable source contracts for Texas Energy ingestion."""

from .bronze import BronzeS3Writer, BronzeSourceTimeRange, BronzeWriteResult
from .backfill import HistoricalBackfillPlan, HistoricalBackfillReport, HistoricalBackfillRunner, HistoricalWindow
from .eia import EiaDemandClient, EiaDemandQuery
from .errors import EmptySourceResponseError
from .orchestration import BronzeIngestionResult, IngestionOrchestrator
from .open_meteo import TexasCity, WeatherClient

__all__ = [
    "BronzeS3Writer",
    "BronzeSourceTimeRange",
    "BronzeWriteResult",
    "HistoricalBackfillPlan",
    "HistoricalBackfillReport",
    "HistoricalBackfillRunner",
    "HistoricalWindow",
    "BronzeIngestionResult",
    "EmptySourceResponseError",
    "EiaDemandClient",
    "EiaDemandQuery",
    "IngestionOrchestrator",
    "TexasCity",
    "WeatherClient",
]
