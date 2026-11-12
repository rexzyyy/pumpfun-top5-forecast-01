import json
import math
import os
import random
from datetime import datetime
from pathlib import Path


def cap_log10(record):
    lamports = max(1, int(record["market_cap_lamports"]))
    return math.log10(lamports) - 9.0


def to_epoch(iso_text):
    return datetime.fromisoformat(
        iso_text.replace("Z", "+00:00")
    ).timestamp()


def run_forecast():
    source = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
    destination = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
    config = json.loads((source / "task.json").read_text())
    snapshots = [
        json.loads(line)
        for line in (source / "history.ndjson").read_text().splitlines()
        if line
    ]
    snapshots = [
        snapshot for snapshot in snapshots
        if snapshot.get("ok") and snapshot.get("rows") is not None
    ]
    if not snapshots:
        raise ValueError("history has no usable marks")

    forecast_horizon = max(
        1.0 / 6.0,
        (to_epoch(config["reads"][-1]) - to_epoch(snapshots[-1]["at"]))
        / 3600.0,
    )
    final_mark_time = to_epoch(snapshots[-1]["at"])
    tracks = {}
    for mark_number, snapshot in enumerate(snapshots):
        for coin in snapshot["rows"]:
            tracks.setdefault(coin["mint"], []).append((mark_number, coin))

    # Build return samples and hazard tallies from adjacent observed records.
    by_venue = {venue: [] for venue in ("curve", "migrating", "pool")}
    return_samples = []
    hazard_events = {venue: 0 for venue in by_venue}
    observed_steps = {venue: 0 for venue in by_venue}
    for mint_id, observations in tracks.items():
        observations.sort(key=lambda pair: pair[0])
        for (old_index, earlier), (new_index, later) in zip(
            observations, observations[1:]
        ):
            gap = max(1, new_index - old_index)
            change = (cap_log10(later) - cap_log10(earlier)) / gap
            venue_name = later.get("venue", "curve")
            creation_age = max(
                0.0,
                (to_epoch(later["created_at"]) - to_epoch(snapshots[0]["at"]))
                / 3600.0,
            )
            venue_bucket = (
                "pool" if venue_name == "pool"
                else ("migrating" if venue_name == "migrating" else "curve")
            )
            sample = (
                change, min(8, gap), creation_age,
                earlier.get("venue", "curve"), later.get("venue", "curve"),
            )
            by_venue[venue_bucket].append(sample)
            return_samples.append(sample)
            observed_steps[venue_bucket] += 1
            if change < -0.12 or (abs(change) < 0.006 and gap >= 2):
                hazard_events[venue_bucket] += 1
    if not return_samples:
        return_samples = [(0.0, 1, 0.0, "curve", "curve")]

    hazard_rates = {
        venue: min(
            0.72,
            max(
                0.10,
                hazard_events[venue] / max(1, observed_steps[venue]),
            ),
        )
        for venue in by_venue
    }
    generator = random.Random(713902)
    all_mints = sorted(tracks)
    current = {}
    for mint_id, observations in tracks.items():
        observations.sort(key=lambda pair: pair[0])
        newest = observations[-1][1]
        previous_index = max(0, len(observations) - 4)
        slope = (
            cap_log10(observations[-1][1]) - cap_log10(observations[previous_index][1])
        ) / max(1, len(observations) - 1 - min(3, len(observations) - 1))
        current[mint_id] = {
            "row": newest,
            "x": cap_log10(newest),
            "venue": newest.get("venue", "curve"),
            "age": max(
                0.0,
                (final_mark_time - to_epoch(newest["created_at"])) / 3600.0,
            ),
            "trend": slope,
        }

    # Walk each known coin forward using seeded, venue-matched returns.
    simulation_count = 2400
    place_count = int(config.get("places", 5))
    trajectories = [[{} for _ in range(place_count)] for _ in range(simulation_count)]
    for mint_id, status in current.items():
        venue = status["venue"] if status["venue"] in by_venue else "curve"
        venue_samples = by_venue[venue] or return_samples
        is_young = status["age"] < 1.0
        chunk_hours = min(1.0, forecast_horizon)
        volatility_scale = math.sqrt(chunk_hours * 6.0)
        momentum = max(-0.5, min(0.55, status["trend"] * 6.0))
        average_return = sum(item[0] for item in venue_samples) / len(venue_samples)
        drift = (
            0.35 * momentum
            + 0.65 * max(-0.025, min(0.035, average_return * 6.0))
        )
        crash_chance = min(
            0.82,
            hazard_rates[venue] * min(1.0, forecast_horizon / 2.0)
            * (1.25 if is_young else 0.8),
        )
        for simulation in range(simulation_count):
            projected = status["x"]
            will_crash = generator.random() < crash_chance
            crash_time = generator.random() * forecast_horizon if will_crash else forecast_horizon + 1
            elapsed = 0.0
            while elapsed < forecast_horizon:
                step = min(chunk_hours, forecast_horizon - elapsed)
                picked = venue_samples[generator.randrange(len(venue_samples))]
                # Every sampled change is already expressed per 10-minute mark.
                shock = picked[0] * volatility_scale
                shock = max(-0.9, min(0.9, shock))
                projected += drift * step + shock * 0.48
                elapsed += step
                if will_crash and elapsed >= crash_time:
                    projected += -0.55 * step - 0.08
                    will_crash = False
            trajectories[simulation][0][mint_id] = projected

    # Convert simulated ranks into mint and size-bin probability mass.
    place_chances = {
        place: {mint_id: 0.0 for mint_id in all_mints}
        for place in range(1, place_count + 1)
    }
    bin_chances = {
        place: [0.0] * 82 for place in range(1, place_count + 1)
    }
    for simulation in range(simulation_count):
        ordering = sorted(
            ((value, mint_id) for mint_id, value in trajectories[simulation][0].items()),
            key=lambda pair: (-pair[0], pair[1]),
        )
        for place in range(1, place_count + 1):
            if place <= len(ordering):
                log_cap, mint_id = ordering[place - 1]
                place_chances[place][mint_id] += 1.0 / simulation_count
                bin_number = (
                    0 if log_cap < 2
                    else (81 if log_cap >= 6 else int((log_cap - 2) / 0.05) + 1)
                )
                bin_chances[place][bin_number] += 1.0 / simulation_count

    # Estimate how much top-place traffic is due to coins first observed later.
    transitions = 0
    new_top_five = 0
    new_top_chart = 0
    previously_seen = set()
    for snapshot in snapshots:
        chart_rows = snapshot["rows"]
        new_top_five += sum(
            1 for coin in chart_rows[:5] if coin["mint"] not in previously_seen
        )
        new_top_chart += sum(
            1 for coin in chart_rows if coin["mint"] not in previously_seen
        )
        if previously_seen:
            transitions += 1
        previously_seen.update(coin["mint"] for coin in chart_rows)
    newcomer_share = min(
        0.48,
        (new_top_five / max(1, transitions * 5))
        * min(1.0, forecast_horizon / 2.0),
    )
    named_mass = max(0.54, 1.0 - newcomer_share)

    probability_rows = []
    for place in range(1, place_count + 1):
        leaders = sorted(
            place_chances[place].items(),
            key=lambda pair: (-pair[1], pair[0]),
        )[:100]
        for mint_id, chance in leaders:
            if chance * named_mass >= 0.000001:
                probability_rows.append({
                    "mint": mint_id,
                    "place": place,
                    "p": round(chance * named_mass, 8),
                })

    # Mix simulated sizes with a broad distribution around each current rank.
    size_rows = []
    final_chart = sorted(snapshots[-1]["rows"], key=lambda coin: int(coin["rank"]))
    for place in range(1, place_count + 1):
        simulated = bin_chances[place]
        center = cap_log10(final_chart[place - 1]) if len(final_chart) >= place else 3.0
        span = max(
            0.0,
            (to_epoch(config["reads"][-1]) - final_mark_time) / 3600.0,
        )
        center += min(0.18, 0.018 * span)
        width = min(0.62, 0.24 + 0.10 * math.sqrt(span))
        local_bins = [
            math.exp(-0.5 * ((2.025 + (bin_id - 1) * 0.05 - center) / width) ** 2)
            for bin_id in range(82)
        ]
        local_total = sum(local_bins)
        local_bins = [value / local_total for value in local_bins]
        combined = [
            0.62 * simulated[bin_id] + 0.38 * local_bins[bin_id]
            for bin_id in range(82)
        ]
        smoothed = [0.0] * 82
        for bin_id, chance in enumerate(combined):
            smoothed[bin_id] += 0.70 * chance
            for offset, weight in ((1, 0.12), (2, 0.03)):
                smoothed[max(0, bin_id - offset)] += weight * chance
                smoothed[min(81, bin_id + offset)] += weight * chance
        for bin_id, chance in enumerate(smoothed):
            chance *= named_mass
            if chance > 1e-7:
                size_rows.append({
                    "place": place,
                    "bin": bin_id,
                    "p": round(chance, 8),
                })

    guessed_top_five = sorted(current, key=lambda mint_id: (-current[mint_id]["x"], mint_id))[:5]
    destination.mkdir(parents=True, exist_ok=True)
    forecast = {
        "probabilities": probability_rows,
        "top5": guessed_top_five,
        "sizes": size_rows,
    }
    target = destination / config.get("output", "forecast.json")
    target.write_text(json.dumps(forecast, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    run_forecast()
