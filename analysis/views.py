import json

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from analysis.analytics import mood_timeline


def analysis_overview(request: HttpRequest) -> HttpResponse:
    granularity = request.GET.get("granularity", "month")
    if granularity not in ("week", "month", "quarter"):
        granularity = "month"

    date_from = request.GET.get("date_from", "")
    date_to   = request.GET.get("date_to", "")

    full_timeline = mood_timeline(granularity)

    # Date range bounds for input min/max hints
    min_period = full_timeline[0]["period"]  if full_timeline else ""
    max_period = full_timeline[-1]["period"] if full_timeline else ""

    # Apply date range filter
    timeline = full_timeline
    if date_from:
        timeline = [r for r in timeline if r["period"] >= date_from]
    if date_to:
        timeline = [r for r in timeline if r["period"] <= date_to]

    labels  = [row["period"] for row in timeline]
    valence = [round(row["mean_valence"], 4) if row["mean_valence"] is not None else None for row in timeline]
    energy  = [round(row["mean_energy"],  4) if row["mean_energy"]  is not None else None for row in timeline]
    track_count = sum(row["track_count"] for row in timeline)

    return render(request, "analysis/partials/analysis_overview.html", {
        "granularity":  granularity,
        "granularities": ["week", "month", "quarter"],
        "date_from":    date_from,
        "date_to":      date_to,
        "min_period":   min_period,
        "max_period":   max_period,
        "timeline":     timeline,
        "labels_json":  json.dumps(labels),
        "valence_json": json.dumps(valence),
        "energy_json":  json.dumps(energy),
        "track_count":  track_count,
    })
