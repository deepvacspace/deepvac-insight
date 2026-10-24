"""Analysis Core: stages, KPIs, steady states, saturation, PID shares, anomalies.

Port of control2-client's src/core/analysis (C++). Pure computation, no Qt.
Entry point: analyse_experiment(); stored runs go through runsamples.experiment_input().
"""

from app.core.analysis.experimentanalysis import (
    ANALYSIS_VERSION,
    ExperimentAnalysis,
    ExperimentInput,
    ExperimentOptions,
    analyse_experiment,
)

__all__ = [
    "ANALYSIS_VERSION",
    "ExperimentAnalysis",
    "ExperimentInput",
    "ExperimentOptions",
    "analyse_experiment",
]
