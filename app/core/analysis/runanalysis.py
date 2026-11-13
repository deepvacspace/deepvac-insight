"""Whole-run and per-stage KPIs."""

from dataclasses import dataclass, field, replace

from app.core.analysis.common import NaN, json_number, valid
from app.core.analysis.kpis import ControlKpis, KpiOptions, compute_kpis, control_cost
from app.core.analysis.segmentation import (
    HOLD,
    RAMP,
    SETPOINT,
    Segment,
    SegmentationOptions,
    segment_experiment,
)


@dataclass
class StageAnalysis:
    segment: Segment
    kpis: ControlKpis


@dataclass
class RunAnalysis:
    overall: ControlKpis = field(default_factory=ControlKpis)
    overshoot: float = NaN
    undershoot: float = NaN
    settling_time_s: float = NaN
    cost: float = NaN
    stages: list[StageAnalysis] = field(default_factory=list)

    def has_ramp(self) -> bool:
        """Whether the run has a ramp."""
        return any(s.segment.kind == RAMP for s in self.stages)

    def to_json(self) -> dict:
        stages = []
        for stage in self.stages:
            item = kpis_to_json(stage.kpis)
            item.update(
                {
                    "kind": stage.segment.kind,
                    "start_s": stage.segment.start,
                    "end_s": stage.segment.end,
                    "interval": stage.segment.interval,
                    "cycle": stage.segment.cycle,
                    "reference_start": json_number(stage.segment.reference_start),
                    "reference_end": json_number(stage.segment.reference_end),
                }
            )
            stages.append(item)
        run = kpis_to_json(self.overall)
        run.update(
            {
                "overshoot_worst_step": json_number(self.overshoot),
                "undershoot_worst_step": json_number(self.undershoot),
                "settling_time_last_step_s": json_number(self.settling_time_s),
                "cost": json_number(self.cost),
            }
        )
        return {"kpis": run, "stages": stages}


def tracks_setpoint(kind: str) -> bool:
    """Whether a stage's MAE measures tracking."""
    return kind in (RAMP, HOLD)


def kpis_to_json(kpis: ControlKpis) -> dict:
    return {
        "samples": kpis.samples,
        "mae": json_number(kpis.mae),
        "rmse": json_number(kpis.rmse),
        "bias": json_number(kpis.bias),
        "max_abs_error": json_number(kpis.max_abs_error),
        "overshoot": json_number(kpis.overshoot),
        "overshoot_rms": json_number(kpis.overshoot_rms),
        "undershoot": json_number(kpis.undershoot),
        "rise_time_s": json_number(kpis.rise_time_s),
        "fall_time_s": json_number(kpis.fall_time_s),
        "time_to_near_s": json_number(kpis.time_to_near_s),
        "settling_time_s": json_number(kpis.settling_time_s),
        "steady_state_error": json_number(kpis.steady_state_error),
        "tail_mae": json_number(kpis.tail_mae),
        "tail_bias": json_number(kpis.tail_bias),
        "tail_std": json_number(kpis.tail_std),
        "final_error": json_number(kpis.final_error),
        "min": json_number(kpis.min_value),
        "max": json_number(kpis.max_value),
        "end": json_number(kpis.end_value),
    }


def _kpis_over(times, value, reference, first: int, last: int, options: KpiOptions) -> ControlKpis:
    stage_options = replace(options, tail_end_s=NaN, initial_value=NaN, start_time_s=NaN)
    return compute_kpis(
        times[first : last + 1], value[first : last + 1], reference[first : last + 1], stage_options
    )


def analyse_run(
    times,
    value,
    reference,
    interval=(),
    cycle=(),
    kpi_options: KpiOptions | None = None,
    segmentation_options: SegmentationOptions | None = None,
) -> RunAnalysis:
    kpi_options = kpi_options or KpiOptions()
    segmentation_options = segmentation_options or SegmentationOptions()
    analysis = RunAnalysis(overall=compute_kpis(times, value, reference, kpi_options))
    segments = segment_experiment(times, value, reference, interval, cycle, segmentation_options)
    worst_overshoot = NaN
    worst_undershoot = NaN
    for s, segment in enumerate(segments):
        last = segment.last
        if (
            segment.kind == SETPOINT
            and s + 1 < len(segments)
            and segments[s + 1].kind == HOLD
            and abs(segments[s + 1].reference_start - segment.reference_end)
            < segmentation_options.reference_tolerance
        ):
            last = segments[s + 1].last
        stage = StageAnalysis(
            segment, _kpis_over(times, value, reference, segment.first, last, kpi_options)
        )
        if segment.kind == SETPOINT:
            if valid(stage.kpis.undershoot):
                worst_undershoot = (
                    max(worst_undershoot, stage.kpis.undershoot)
                    if valid(worst_undershoot)
                    else stage.kpis.undershoot
                )
            if valid(stage.kpis.overshoot):
                worst_overshoot = (
                    max(worst_overshoot, stage.kpis.overshoot)
                    if valid(worst_overshoot)
                    else stage.kpis.overshoot
                )
            analysis.settling_time_s = stage.kpis.settling_time_s
        analysis.stages.append(stage)
    has_steps = valid(worst_overshoot)
    analysis.overshoot = worst_overshoot if has_steps else analysis.overall.overshoot
    analysis.undershoot = worst_undershoot if has_steps else analysis.overall.undershoot
    if not has_steps:
        analysis.settling_time_s = analysis.overall.settling_time_s
    for_cost = replace(analysis.overall, overshoot=analysis.overshoot)
    analysis.cost = control_cost(for_cost) if analysis.overall.samples > 0 else NaN
    return analysis
