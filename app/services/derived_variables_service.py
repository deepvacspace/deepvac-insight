"""User-defined derived channels."""

import sqlite3
from datetime import datetime, timezone

import numpy as np

from app.common import DATA_DIR
from app.services import safe_eval

DERIVED_VARIABLES_DB = DATA_DIR / "deepvac_derived_variables.sqlite3"

TYPE_DIFFERENCE = "difference"
TYPE_RATE_OF_CHANGE = "rate_of_change"
TYPE_ROLLING_STD = "rolling_std"
TYPE_CUMULATIVE_INTEGRAL = "cumulative_integral"
TYPE_CUSTOM = "custom"
ALL_TYPES = [
    TYPE_DIFFERENCE,
    TYPE_RATE_OF_CHANGE,
    TYPE_ROLLING_STD,
    TYPE_CUMULATIVE_INTEGRAL,
    TYPE_CUSTOM,
]


class DerivedVariableError(ValueError):
    pass


def connect_derived_variables(db_path=None):
    """Opens the derived variables database."""
    path = db_path or DERIVED_VARIABLES_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS derived_variables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,
            source_channel TEXT,
            source_channel2 TEXT,
            window INTEGER,
            expression TEXT,
            color TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'Unknown',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "source_channel": row["source_channel"],
        "source_channel2": row["source_channel2"],
        "window": row["window"],
        "expression": row["expression"],
        "color": row["color"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def list_derived_variables():
    conn = connect_derived_variables()
    try:
        rows = conn.execute("SELECT * FROM derived_variables ORDER BY name").fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def _validate_definition(name, var_type, source_channel, source_channel2, window, expression):
    if not name or not name.strip():
        raise DerivedVariableError("Name is required.")
    if var_type not in ALL_TYPES:
        raise DerivedVariableError(f"Unknown derived variable type: {var_type!r}")
    if var_type in (TYPE_DIFFERENCE,) and not (source_channel and source_channel2):
        raise DerivedVariableError("'difference' needs two source channels.")
    if (
        var_type in (TYPE_RATE_OF_CHANGE, TYPE_ROLLING_STD, TYPE_CUMULATIVE_INTEGRAL)
        and not source_channel
    ):
        raise DerivedVariableError(f"'{var_type}' needs a source channel.")
    if var_type == TYPE_ROLLING_STD and (not window or int(window) < 2):
        raise DerivedVariableError("'rolling_std' needs a window of at least 2 samples.")
    if var_type == TYPE_CUSTOM:
        if not expression or not expression.strip():
            raise DerivedVariableError("A custom derived variable needs an expression.")
        try:
            safe_eval.referenced_names(expression)
        except safe_eval.SafeEvalError as exc:
            raise DerivedVariableError(str(exc)) from exc


def add_derived_variable(
    name,
    var_type,
    *,
    source_channel=None,
    source_channel2=None,
    window=None,
    expression=None,
    color="#60a5fa",
    created_by="Unknown",
):
    _validate_definition(name, var_type, source_channel, source_channel2, window, expression)
    conn = connect_derived_variables()
    try:
        try:
            cur = conn.execute(
                """
                INSERT INTO derived_variables
                    (name, type, source_channel, source_channel2, window, expression, color,
                     created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    var_type,
                    source_channel,
                    source_channel2,
                    int(window) if window else None,
                    expression,
                    color,
                    created_by or "Unknown",
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DerivedVariableError(
                f"A derived variable named '{name}' already exists."
            ) from exc
        conn.commit()
        row = conn.execute(
            "SELECT * FROM derived_variables WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row(row)
    finally:
        conn.close()


def delete_derived_variable(variable_id):
    conn = connect_derived_variables()
    try:
        conn.execute("DELETE FROM derived_variables WHERE id = ?", (variable_id,))
        conn.commit()
    finally:
        conn.close()


def required_channels(definition):
    """Returns the channels a definition needs."""
    if definition["type"] == TYPE_DIFFERENCE:
        return {definition["source_channel"], definition["source_channel2"]}
    if definition["type"] in (TYPE_RATE_OF_CHANGE, TYPE_ROLLING_STD, TYPE_CUMULATIVE_INTEGRAL):
        return {definition["source_channel"]}
    if definition["type"] == TYPE_CUSTOM:
        return safe_eval.referenced_names(definition["expression"])
    return set()


def missing_channels(definition, available_columns):
    return required_channels(definition) - set(available_columns)


def compute_series(definition, columns_data, elapsed_seconds):
    """Computes a derived channel from a run's columns."""
    missing = missing_channels(definition, columns_data.keys())
    if missing:
        raise DerivedVariableError(
            f"'{definition['name']}' needs channel(s) not present in this run: "
            f"{', '.join(sorted(missing))}"
        )

    var_type = definition["type"]
    try:
        if var_type == TYPE_DIFFERENCE:
            a = columns_data[definition["source_channel"]]
            b = columns_data[definition["source_channel2"]]
            return a - b

        if var_type == TYPE_RATE_OF_CHANGE:
            values = columns_data[definition["source_channel"]]
            return _rate_of_change(values, elapsed_seconds)

        if var_type == TYPE_ROLLING_STD:
            values = columns_data[definition["source_channel"]]
            return _rolling_std(values, int(definition["window"]))

        if var_type == TYPE_CUMULATIVE_INTEGRAL:
            values = columns_data[definition["source_channel"]]
            return _cumulative_integral(values, elapsed_seconds)

        if var_type == TYPE_CUSTOM:
            result = safe_eval.evaluate(definition["expression"], dict(columns_data))
            return np.broadcast_to(result, elapsed_seconds.shape).astype(float)

    except safe_eval.SafeEvalError as exc:
        raise DerivedVariableError(str(exc)) from exc
    except (ZeroDivisionError, ValueError, TypeError, ArithmeticError) as exc:
        raise DerivedVariableError(f"Could not compute '{definition['name']}': {exc}") from exc

    raise DerivedVariableError(f"Unknown derived variable type: {var_type!r}")


def _rate_of_change(values, elapsed_seconds):
    values = np.asarray(values, dtype=float)
    t = np.asarray(elapsed_seconds, dtype=float)
    dt = np.diff(t, prepend=t[0] - 1.0 if len(t) else 0.0)
    dt[dt == 0] = np.nan
    dv = np.diff(values, prepend=values[0] if len(values) else 0.0)
    return dv / dt


def _rolling_std(values, window):
    values = np.asarray(values, dtype=float)
    n = len(values)
    out = np.full(n, np.nan)
    for i in range(n):
        start = max(0, i - window + 1)
        segment = values[start : i + 1]
        segment = segment[~np.isnan(segment)]
        if len(segment) >= 2:
            out[i] = float(np.std(segment, ddof=1))
    return out


def _cumulative_integral(values, elapsed_seconds):
    values = np.asarray(values, dtype=float)
    t = np.asarray(elapsed_seconds, dtype=float)
    if len(values) == 0:
        return values
    safe_values = np.nan_to_num(values, nan=0.0)
    return np.concatenate(
        ([0.0], np.cumsum(np.diff(t) * (safe_values[:-1] + safe_values[1:]) / 2.0))
    )
