"""Conversion of stored run samples to analysis input."""

import json

from app.core.analysis.common import NaN
from app.core.analysis.experimentanalysis import ExperimentInput

NON_CHANNEL_COLUMNS = {"run_id", "timestamp", "start_temp"}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def columns_from_rows(rows: list[dict]) -> tuple[list[float], dict[str, list[float]]]:
    """Returns the times and channels of sample rows."""
    times: list[float] = []
    channels: dict[str, list[float]] = {}
    for row in rows:
        timestamp = row.get("timestamp")
        if not _is_number(timestamp):
            continue
        index = len(times)
        times.append(float(timestamp))
        for key, value in row.items():
            if key in NON_CHANNEL_COLUMNS or not (value is None or _is_number(value)):
                continue
            column = channels.get(key)
            if column is None:
                column = channels[key] = [NaN] * index
            elif len(column) < index:
                column.extend([NaN] * (index - len(column)))
            column.append(NaN if value is None else float(value))
        for column in channels.values():
            if len(column) < index + 1:
                column.extend([NaN] * (index + 1 - len(column)))
    return times, channels


def experiment_input(rows: list[dict], rules=()) -> ExperimentInput:
    """Builds the analysis input of sample rows."""
    times, channels = columns_from_rows(rows)
    start = times[0] if times else 0.0
    return ExperimentInput(times=[t - start for t in times], channels=channels, rules=list(rules))


def experiment_input_from_json(samples_json: str, rules=()) -> ExperimentInput:
    return experiment_input(json.loads(samples_json), rules)
