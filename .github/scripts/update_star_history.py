#!/usr/bin/env python3
"""Build account-wide star history using only Python's standard library.

Every run discovers ALL owned public repositories, including forks and archives.
The curve sums GitHub-recorded daily additions; removed stars cannot be inferred
from this API. The current star count is kept separately, never used to rescale
history. No stargazer identities or access tokens are saved.

Run: GITHUB_TOKEN=... python3 .github/scripts/update_star_history.py --owner NAME
API: https://docs.github.com/en/rest/activity/starring#get-repository-star-history
"""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


API = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[2]
PALETTES = {
    "light": ("#ffffff", "#1f2328", "#59636e", "#0969da", "#d8dee4"),
    "dark": ("#0d1117", "#e6edf3", "#9198a1", "#58a6ff", "#30363d"),
}


class GitHub:
    def __init__(self, token=""):
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "account-star-history",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def pages(self, path, **params):
        url = f"{API}{path}?{urlencode(params)}"
        seen = set()
        while url:
            # Never forward the token to a host from an unexpected pagination URL.
            if urlparse(url).scheme != "https" or urlparse(url).netloc != "api.github.com":
                raise ValueError("Unexpected GitHub pagination host")
            if url in seen:
                raise ValueError("Repeated GitHub pagination URL")
            seen.add(url)
            for attempt in range(3):
                try:
                    with urlopen(Request(url, headers=self.headers), timeout=30) as response:
                        payload = json.load(response)
                        links = response.headers.get("Link", "")
                    break
                except HTTPError as error:
                    retryable = error.code in (429, 500, 502, 503, 504) or (
                        error.code == 403 and (
                            error.headers.get("Retry-After")
                            or error.headers.get("X-RateLimit-Remaining") == "0"
                        )
                    )
                    delay = max(2 ** (attempt + 1), int(error.headers.get("Retry-After", "0")))
                    if not retryable or attempt == 2 or delay > 30:
                        raise RuntimeError(
                            f"GitHub returned HTTP {error.code} for {url}; previous charts were kept"
                        ) from error
                    if error.headers.get("X-RateLimit-Remaining") == "0":
                        raise RuntimeError("GitHub API rate limit reached; retry after it resets") from error
                    time.sleep(delay)
                except (URLError, TimeoutError) as error:
                    if attempt == 2:
                        raise RuntimeError(f"Could not reach GitHub for {url}") from error
                    time.sleep(2 ** (attempt + 1))
            if not isinstance(payload, list):
                raise ValueError(f"Expected a list from {url}")
            yield from payload
            following = re.search(r'<([^>]+)>;\s*rel="next"', links)
            url = following.group(1) if following else None


def collect_history(client, owner, today):
    repositories = []
    daily = Counter()
    discovered = client.pages(
        f"/users/{quote(owner, safe='')}/repos", type="owner", sort="full_name", per_page=100
    )
    for repo in discovered:
        if repo["private"] or repo["owner"]["login"].casefold() != owner.casefold():
            continue
        name = repo["full_name"]
        history = list(client.pages(
            f"/repos/{quote(name, safe='/')}/stargazers/history", per_page=30
        ))
        if not history and repo["stargazers_count"]:
            raise ValueError(f"Missing star history for {name}; refusing to publish partial data")
        seen_weeks = set()
        for week in history:
            timestamp = week["week"]
            days = week["days"]
            if timestamp in seen_weeks:
                raise ValueError(f"Duplicate history week for {name}")
            seen_weeks.add(timestamp)
            if len(days) != 7 or any(type(count) is not int or count < 0 for count in days):
                raise ValueError(f"Invalid daily star counts for {name}")
            if sum(days) != week["total"]:
                raise ValueError(f"Inconsistent weekly star total for {name}")
            # Preserve the API's bucket dates, whose boundaries may differ from UTC.
            week_start = datetime.fromtimestamp(timestamp, timezone.utc)
            for offset, count in enumerate(days):
                day = (week_start + timedelta(days=offset)).date()
                if day > today:
                    if count:
                        raise ValueError(f"Unexpected future star count for {name}")
                    continue
                if count:
                    daily[day] += count
        repositories.append({
            "name": name,
            "current_stars": repo["stargazers_count"],
            "created_at": repo["created_at"],
        })
        print(f"Read {name}: {repo['stargazers_count']} current stars", flush=True)

    repositories.sort(key=lambda repo: repo["name"].casefold())
    earliest_repo = min((date.fromisoformat(repo["created_at"][:10]) for repo in repositories), default=today)
    # Avoid years of empty space before the first recorded star. Keep a zero baseline.
    start = min(daily) - timedelta(days=1) if daily else min(earliest_repo, today - timedelta(days=1))
    points = []
    cumulative = 0
    day = start
    while day <= today:
        additions = daily[day]
        cumulative += additions
        points.append({"date": day.isoformat(), "added": additions, "total": cumulative})
        day += timedelta(days=1)
    return {
        "owner": owner,
        "updated_at": today.isoformat(),
        "source": "https://docs.github.com/en/rest/activity/starring#get-repository-star-history",
        "description": "Cumulative recorded additions across all currently owned public repositories, including forks and archives. Unstars are not subtracted. API day boundaries may differ from UTC.",
        "current_stars": sum(repo["current_stars"] for repo in repositories),
        "recorded_stars": cumulative,
        "repositories": repositories,
        "history": points,
    }


def render_chart(data, theme, mobile=False):
    background, foreground, muted, accent, grid = PALETTES[theme]
    width, height = (480, 330) if mobile else (960, 360)
    left, right, top, bottom = 48, width - 24, 112, height - 64
    points = data["history"]
    last = max(data["recorded_stars"], 1)
    raw_step = last / 4
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = next(n * magnitude for n in (1, 2, 2.5, 5, 10) if n * magnitude >= raw_step)
    ceiling = math.ceil(last / step) * step
    if ceiling == last:
        ceiling += step
    first_day = date.fromisoformat(points[0]["date"])
    final_day = date.fromisoformat(points[-1]["date"])
    duration = max((final_day - first_day).days, 1)

    def x(day):
        return left + (date.fromisoformat(day) - first_day).days / duration * (right - left)

    def y(total):
        return bottom - total / ceiling * (bottom - top)

    def label(value):
        return f"{value / 1000000:g}m" if value >= 1000000 else f"{value / 1000:g}k" if value >= 1000 else f"{value:g}"

    owner = escape(data["owner"])
    repo_count = len(data["repositories"])
    current = data["current_stars"]
    recorded = data["recorded_stars"]
    description = escape(
        f"{owner}: {current} current stars across {repo_count} public repositories. "
        f"The single line shows {recorded} cumulative recorded star additions from "
        f"{points[0]['date']} through {data['updated_at']}. Unstars are not subtracted. "
        "Includes owned forks and archived repositories; refreshed daily."
    )
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{owner} — Star history</title>',
        f'  <desc id="desc">{description}</desc>',
        f'  <rect width="{width}" height="{height}" fill="{background}"/>',
        '  <g font-family="Segoe UI, Helvetica, Arial, sans-serif">',
        f'    <text x="0" y="35" fill="{foreground}" font-size="28" font-weight="600">Star history</text>',
        f'    <text x="0" y="65" fill="{muted}" font-size="18">All {repo_count} public repositories</text>',
        f'    <text x="{right}" y="36" text-anchor="end" fill="{accent}" font-size="32" font-weight="600">{current:,}</text>',
        f'    <text x="{right}" y="65" text-anchor="end" fill="{muted}" font-size="16">stars now</text>',
        f'    <text x="{left}" y="96" fill="{muted}" font-size="14">Cumulative additions</text>',
    ]
    for index in range(math.floor(ceiling / step) + 1):
        value = index * step
        # Fractional ticks make a zero- or one-star chart harder to read.
        if value != int(value):
            continue
        py = y(value)
        svg.extend([
            f'    <path d="M{left} {py:.2f}H{right}" fill="none" stroke="{grid}" stroke-width="1"/>',
            f'    <text x="{left - 12}" y="{py + 5:.2f}" text-anchor="end" fill="{muted}" font-size="16">{label(value)}</text>',
        ])

    # A daily step line preserves recorded jumps, without inventing smooth growth.
    line = f"M{left} {y(points[0]['total']):.2f}"
    previous = points[0]["total"]
    for point in points[1:]:
        if point["total"] != previous:
            line += f"H{x(point['date']):.2f}V{y(point['total']):.2f}"
            previous = point["total"]
    line += f"H{right}"
    svg.extend([
        f'    <path d="{line}" fill="none" stroke="{accent}" stroke-width="2.5" stroke-linejoin="round"/>',
        f'    <circle cx="{right}" cy="{y(recorded):.2f}" r="4" fill="{accent}"/>',
    ])
    tick_count = 3 if mobile else 5
    tick_days = sorted({round(duration * index / (tick_count - 1)) for index in range(tick_count)})
    for index, offset in enumerate(tick_days):
        day = first_day + timedelta(days=offset)
        alignment = "start" if index == 0 else "end" if index == len(tick_days) - 1 else "middle"
        text = day.strftime("%b ") + str(day.day) if duration < 180 else day.strftime("%b %Y")
        svg.append(f'    <text x="{x(day.isoformat()):.2f}" y="{bottom + 28}" text-anchor="{alignment}" fill="{muted}" font-size="16">{text}</text>')
    svg.extend([
        f'    <text x="0" y="{height - 8}" fill="{muted}" font-size="14">Updated {data["updated_at"]}</text>',
        f'    <text x="{right}" y="{height - 8}" text-anchor="end" fill="{muted}" font-size="14">Refreshed daily</text>',
        '  </g>',
        '</svg>',
    ])
    return "\n".join(svg) + "\n"


def write_outputs(data, root):
    # Fetch and render everything before replacing any previously working output.
    outputs = {"data/star-history.json": json.dumps(data, indent=2, ensure_ascii=False) + "\n"}
    for theme in PALETTES:
        for mobile in (False, True):
            suffix = f"mobile-{theme}" if mobile else theme
            outputs[f"assets/star-history-{suffix}.svg"] = render_chart(data, theme, mobile)
    for name, content in outputs.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "hyphy-void"))
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,38}", args.owner):
        parser.error("owner must be a GitHub username")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    data = collect_history(GitHub(token), args.owner, datetime.now(timezone.utc).date())
    write_outputs(data, ROOT)
    print(f"Generated history for {len(data['repositories'])} public repositories ({data['current_stars']} stars)")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError) as error:
        print(f"Star history update failed: {error}", file=sys.stderr)
        sys.exit(1)
