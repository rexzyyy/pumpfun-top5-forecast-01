import datetime as dt
import json
import math
import os
from pathlib import Path


INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def logcap(row):
    return math.log10(int(row["market_cap_lamports"])) - 9.0


def normal_tail(x):
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def main():
    task = json.loads((INPUT_DIR / "task.json").read_text())
    marks = [
        json.loads(line)
        for line in (INPUT_DIR / "history.ndjson").read_text().splitlines()
        if line
    ]
    marks = [
        mark
        for mark in marks
        if mark.get("ok") is True and isinstance(mark.get("rows"), list)
    ]
    t0 = parse_time(task["t0"])
    history_until = parse_time(task["history_until"])
    reads = [parse_time(value) for value in task["reads"]]
    horizon_hours = max(
        0.0, (max(reads) - history_until).total_seconds() / 3600.0
    )
    span_hours = max(0.0, (history_until - t0).total_seconds() / 3600.0)

    tracks = {}
    for mark in marks:
        marked_at = parse_time(mark["at"])
        for row in mark["rows"]:
            tracks.setdefault(row["mint"], []).append(
                (marked_at, logcap(row), row)
            )
    if not tracks:
        raise ValueError("history has no ranked coins")

    candidates = []
    for mint, observations in tracks.items():
        observations.sort(key=lambda item: item[0])
        latest_at, latest_cap, latest_row = observations[-1]
        if len(observations) > 1:
            old_at, old_cap, _ = observations[0]
            elapsed = max(
                (latest_at - old_at).total_seconds() / 3600.0, 1.0 / 60.0
            )
            raw_rate = (latest_cap - old_cap) / elapsed
            clamped_rate = max(-0.45, min(0.75, raw_rate))
            reliability = min(1.0, elapsed / 0.30) * min(
                1.0, len(observations) / 3.0
            )
            rate = clamped_rate * reliability * 0.42
        else:
            rate = 0.0

        age_hours = max(
            0.0,
            (history_until - parse_time(latest_row["created_at"])).total_seconds()
            / 3600.0,
        )
        venue = latest_row.get("venue", "curve")
        pool_bonus = (
            0.24 if venue == "pool" else (-0.10 if venue == "migrating" else -0.58)
        )
        maturity = min(1.0, age_hours / 0.30)
        projected = latest_cap + rate * horizon_hours + pool_bonus + 0.10 * maturity
        candidates.append(
            {
                "mint": mint,
                "row": latest_row,
                "projected": projected,
            }
        )
    candidates.sort(key=lambda item: (-item["projected"], item["mint"].encode("utf-8")))

    places = int(task["places"])
    edges = task["size"]["edges"]
    horizon_scale = math.log10(1.0 + horizon_hours)
    temp = 1.00 + 2.8 * horizon_scale + 0.10 / math.sqrt(1.0 + span_hours)
    named_mass = max(0.16, min(0.68, 0.58 / (1.0 + 0.55 * horizon_scale)))
    persistence_weight = max(
        0.15, 1.0 - 0.60 * max(0, len(marks) - 4)
    )
    last_chart = (
        sorted(marks[-1]["rows"], key=lambda row: int(row["rank"]))
        if marks
        else []
    )
    last_ranks = {row["mint"]: int(row["rank"]) for row in last_chart}

    probabilities = []
    expected_caps = []
    for place in range(1, places + 1):
        projected_weights = [
            math.exp(-abs(index - (place - 1)) / temp)
            for index in range(len(candidates))
        ]
        projected_total = sum(projected_weights)
        vals = []
        for index, candidate in enumerate(candidates):
            rank = last_ranks.get(candidate["mint"])
            observed = (
                0.30
                if rank == place
                else (
                    0.10
                    if rank is not None and abs(rank - place) == 1
                    else 0.0
                )
            )
            value = (
                persistence_weight * observed
                + (1.0 - persistence_weight)
                * named_mass
                * projected_weights[index]
                / projected_total
            )
            vals.append(value)
        if place == 1:
            vals = [0.75 * value for value in vals]

        ranked = sorted(
            zip(vals, candidates),
            key=lambda pair: (-pair[0], pair[1]["mint"].encode("utf-8")),
        )
        selected = ranked[: min(8, len(ranked))]
        for probability, candidate in selected:
            if probability >= 0.000001:
                probabilities.append(
                    {
                        "mint": candidate["mint"],
                        "place": place,
                        "p": round(probability, 8),
                    }
                )

        named_here = {
            row["mint"] for row in probabilities if row["place"] == place
        }
        for row in last_chart[:5]:
            if row["mint"] not in named_here and row["mint"] in tracks:
                probabilities.append(
                    {"mint": row["mint"], "place": place, "p": 0.015}
                )
                named_here.add(row["mint"])

        holder_cap = ranked[0][1]["projected"]
        next_cap = candidates[min(place, len(candidates) - 1)]["projected"]
        expected_caps.append(0.20 * holder_cap + 0.80 * next_cap)

    sizes = []
    size_sd = 0.30 + 0.30 * horizon_scale
    for place, mean in enumerate(expected_caps, 1):
        for bin_index in range(len(edges) + 1):
            lo = -math.inf if bin_index == 0 else float(edges[bin_index - 1])
            hi = math.inf if bin_index == len(edges) else float(edges[bin_index])
            probability = normal_tail((lo - mean) / size_sd) - normal_tail(
                (hi - mean) / size_sd
            )
            probability = math.floor(probability * 1_000_000) / 1_000_000
            if probability > 0:
                sizes.append(
                    {"place": place, "bin": bin_index, "p": probability}
                )

    output = {
        "probabilities": probabilities,
        "top5": [candidate["mint"] for candidate in candidates[:places]],
        "sizes": sizes,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / task["output"]).write_text(
        json.dumps(output, separators=(",", ":"), ensure_ascii=False) + chr(10)
    )


if __name__ == "__main__":
    main()
