"""Controller output saturation and PID term shares (port of core/analysis/controlanalysis.cpp)."""

from dataclasses import dataclass

from app.core.analysis.common import NaN, valid
from app.core.analysis.seriesstats import percentile


@dataclass
class SaturationOptions:
    u_min: float = -100.0  # control output limits (temp_u, %)
    u_max: float = 100.0
    tolerance: float = 0.5  # within this of a limit counts as saturated


@dataclass
class SaturationStats:
    samples: int = 0  # valid output samples
    fraction: float = NaN  # share of time at either limit, 0 ... 1
    fraction_high: float = NaN
    fraction_low: float = NaN
    total_s: float = 0.0  # time at a limit
    longest_s: float = NaN  # longest continuous episode
    episodes: int = 0
    windup_s: float = NaN  # saturated while the integral term keeps pushing further
    u_mean: float = NaN
    u_min: float = NaN
    u_max: float = NaN


@dataclass
class SaturationEpisode:
    start: float = 0.0
    end: float = 0.0
    high: bool = True


@dataclass
class PidContribution:
    samples: int = 0
    mean_p: float = NaN  # signed means
    mean_i: float = NaN
    mean_d: float = NaN
    mean_abs_p: float = NaN
    mean_abs_i: float = NaN
    mean_abs_d: float = NaN
    share_p: float = NaN  # mean |term| / sum of mean |terms|, 0 ... 1
    share_i: float = NaN
    share_d: float = NaN

    def dominant(self) -> str:
        """ "P", "I", "D" or empty."""
        if not (valid(self.share_p) and valid(self.share_i) and valid(self.share_d)):
            return ""
        if self.share_p >= self.share_i and self.share_p >= self.share_d:
            return "P"
        return "I" if self.share_i >= self.share_d else "D"


def _sample_durations(times, first: int, last: int) -> list[float]:
    """Time a sample stands for: up to the next sample, gaps capped at a few typical periods."""
    steps = [times[k + 1] - times[k] for k in range(first, last)]
    typical = max(percentile(steps, 0.5), 1e-9) if steps else 1.0
    durations = [typical] * (last - first + 1)
    for k in range(first, last):
        durations[k - first] = min(times[k + 1] - times[k], 5.0 * typical)
    return durations


def analyse_saturation(
    times,
    u,
    integral,
    first: int,
    last: int,
    options: SaturationOptions | None = None,
    episodes: list | None = None,
) -> SaturationStats:
    """[first, last] sample range (inclusive, clamped); integral (temp_u_i) may be empty."""
    options = options or SaturationOptions()
    stats = SaturationStats()
    first = max(first, 0)
    last = min(last, min(len(times), len(u)) - 1)
    if last < first:
        return stats
    durations = _sample_durations(times, first, last)
    with_integral = len(integral) > last
    total = high = low = windup = total_u = 0.0
    longest = 0.0
    episode_start = NaN
    episode_side = 0  # +1 high, -1 low, 0 none

    def close_episode(end: float) -> None:
        nonlocal longest, episode_side
        if episode_side != 0:
            longest = max(longest, end - episode_start)
            stats.episodes += 1
            if episodes is not None:
                episodes.append(SaturationEpisode(episode_start, end, episode_side > 0))
        episode_side = 0

    for k in range(first, last + 1):
        value = u[k]
        if not valid(value):
            close_episode(times[k])
            continue
        dt = durations[k - first]
        stats.samples += 1
        total += dt
        total_u += value
        stats.u_min = min(stats.u_min, value) if valid(stats.u_min) else value
        stats.u_max = max(stats.u_max, value) if valid(stats.u_max) else value
        side = (
            1
            if value >= options.u_max - options.tolerance
            else (-1 if value <= options.u_min + options.tolerance else 0)
        )
        if side > 0:
            high += dt
        elif side < 0:
            low += dt
        if side != episode_side:
            close_episode(times[k])
            if side != 0:
                episode_side = side
                episode_start = times[k]
        if (
            side != 0
            and with_integral
            and k > first
            and valid(integral[k])
            and valid(integral[k - 1])
            and (integral[k] - integral[k - 1]) * side > 0.0
        ):
            windup += dt
    close_episode(times[last])
    if stats.samples > 0:
        stats.fraction_high = high / total
        stats.fraction_low = low / total
        stats.fraction = (high + low) / total
        stats.total_s = high + low
        stats.longest_s = longest
        stats.u_mean = total_u / stats.samples
        stats.windup_s = windup if with_integral else NaN
    return stats


def analyse_pid_contribution(p, i, d, first: int, last: int) -> PidContribution:
    contribution = PidContribution()
    first = max(first, 0)
    last = min(last, min(len(p), len(i), len(d)) - 1)
    sum_p = sum_i = sum_d = abs_p = abs_i = abs_d = 0.0
    for k in range(first, last + 1):
        if not (valid(p[k]) and valid(i[k]) and valid(d[k])):
            continue
        contribution.samples += 1
        sum_p += p[k]
        sum_i += i[k]
        sum_d += d[k]
        abs_p += abs(p[k])
        abs_i += abs(i[k])
        abs_d += abs(d[k])
    if contribution.samples == 0:
        return contribution
    n = float(contribution.samples)
    contribution.mean_p = sum_p / n
    contribution.mean_i = sum_i / n
    contribution.mean_d = sum_d / n
    contribution.mean_abs_p = abs_p / n
    contribution.mean_abs_i = abs_i / n
    contribution.mean_abs_d = abs_d / n
    total = abs_p + abs_i + abs_d
    if total > 0.0:
        contribution.share_p = abs_p / total
        contribution.share_i = abs_i / total
        contribution.share_d = abs_d / total
    return contribution
