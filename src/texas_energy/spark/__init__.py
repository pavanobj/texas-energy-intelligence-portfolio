"""Optional local Spark and Delta Lake execution adapters."""

from .silver import SparkDependencyError, run_local_silver_job

__all__ = ["SparkDependencyError", "run_local_silver_job"]
