"""Offline checks for discovery, pagination, aggregation and safe publication."""

from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

from update_star_history import GitHub, collect_history, write_outputs


TODAY = date(2026, 11, 4)
OWNER = "hyphy-void"


def repository(name, stars=0, **extra):
    return {
        "full_name": f"{OWNER}/{name}", "owner": {"login": OWNER},
        "private": False, "fork": False, "archived": False,
        "created_at": "2026-10-01T12:00:00Z", "stargazers_count": stars, **extra,
    }


def week(days):
    return {
        "week": int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()),
        "days": days, "total": sum(days),
    }


class FakeGitHub:
    def __init__(self, repos, histories=None):
        self.repos = repos
        self.histories = histories or {}
        self.calls = []

    def pages(self, path, **params):
        self.calls.append(path)
        if path.startswith("/users/"):
            return iter(self.repos)
        name = path.split("/")[3]
        return iter(self.histories.get(name, []))


class Response(StringIO):
    def __init__(self, values, links=""):
        super().__init__(json.dumps(values))
        self.headers = {"Link": links}


class HistoryTests(unittest.TestCase):
    def collect(self, client):
        with redirect_stdout(StringIO()):
            return collect_history(client, OWNER, TODAY)

    def test_sums_repositories_by_date_and_keeps_current_count_separate(self):
        client = FakeGitHub(
            [repository("a", 2), repository("b", 4)],
            {"a": [week([0, 1, 2, 0, 0, 0, 0])], "b": [week([2, 0, 3, 0, 0, 0, 0])]},
        )
        data = self.collect(client)
        self.assertEqual(data["current_stars"], 6)
        self.assertEqual(data["recorded_stars"], 8)
        self.assertEqual([point["total"] for point in data["history"]], [0, 2, 3, 8, 8])
        self.assertEqual(data["history"][0]["date"], "2026-10-31")
        self.assertEqual(data["history"][-1]["date"], TODAY.isoformat())

    def test_new_public_repositories_are_discovered_on_every_run(self):
        client = FakeGitHub([repository("existing")])
        self.assertEqual(len(self.collect(client)["repositories"]), 1)
        client.repos.append(repository("new", 1))
        client.histories["new"] = [week([0, 0, 0, 1, 0, 0, 0])]
        data = self.collect(client)
        self.assertEqual(len(data["repositories"]), 2)
        self.assertEqual(data["recorded_stars"], 1)
        self.assertEqual(sum(path.startswith("/users/") for path in client.calls), 2)

    def test_includes_owned_forks_and_archives_but_excludes_private_and_other_owners(self):
        client = FakeGitHub([
            repository("fork", fork=True), repository("archive", archived=True),
            repository("secret", private=True), repository("other", owner={"login": "someone"}),
        ])
        data = self.collect(client)
        self.assertEqual([repo["name"] for repo in data["repositories"]], [f"{OWNER}/archive", f"{OWNER}/fork"])
        self.assertFalse(any("secret" in path or "other/" in path for path in client.calls))

    def test_has_no_twenty_repository_limit(self):
        data = self.collect(FakeGitHub([repository(f"repo-{index}") for index in range(205)]))
        self.assertEqual(len(data["repositories"]), 205)

    def test_reads_history_even_for_repositories_with_no_current_stars(self):
        data = self.collect(FakeGitHub([repository("unstarred")], {"unstarred": [week([1, 0, 0, 0, 0, 0, 0])]}))
        self.assertEqual(data["current_stars"], 0)
        self.assertEqual(data["recorded_stars"], 1)

    def test_rejects_missing_or_invalid_history_instead_of_publishing_partial_counts(self):
        bad_histories = [[], [week([1, 0, 0, 0, 0, 0, 0])] * 2, [week([0, 0, 0, 0, 1, 0, 0])]]
        for history in bad_histories:
            with self.subTest(history=history), self.assertRaises(ValueError):
                self.collect(FakeGitHub([repository("a", 1)], {"a": history}))

    def test_zero_one_and_many_star_outputs_are_valid_and_deterministic(self):
        for count in (0, 1, 97, 1000000):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                history = [week([0, count, 0, 0, 0, 0, 0])] if count else []
                data = self.collect(FakeGitHub([repository("a", count)], {"a": history}))
                root = Path(directory)
                write_outputs(data, root)
                before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                self.assertEqual(len(before), 5)
                self.assertEqual(json.loads((root / "data/star-history.json").read_text(encoding="utf-8")), data)
                for path in (root / "assets").glob("*.svg"):
                    svg = ET.parse(path).getroot()
                    self.assertIsNotNone(svg.find("{http://www.w3.org/2000/svg}desc"))
                    self.assertNotIn("nan", path.read_text(encoding="utf-8").lower())
                write_outputs(data, root)
                self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})


class PaginationTests(unittest.TestCase):
    def test_follows_next_link_until_last_page(self):
        next_url = "https://api.github.com/users/hyphy-void/repos?per_page=100&page=2"
        responses = [Response(list(range(100)), f'<{next_url}>; rel="next"'), Response(list(range(100, 205)))]
        with patch("update_star_history.urlopen", side_effect=responses) as request:
            values = list(GitHub("test-token").pages(f"/users/{OWNER}/repos", per_page=100))
        self.assertEqual(values, list(range(205)))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args[0].full_url, next_url)

    def test_failing_later_page_is_an_error(self):
        url = "https://api.github.com/repos/hyphy-void/a/stargazers/history?page=2"
        responses = [Response([week([1, 0, 0, 0, 0, 0, 0])], f'<{url}>; rel="next"'), HTTPError(url, 404, "Not found", {}, None)]
        with patch("update_star_history.urlopen", side_effect=responses), self.assertRaises(RuntimeError):
            list(GitHub().pages(f"/repos/{OWNER}/a/stargazers/history"))

    def test_rejects_external_pagination_link_before_forwarding_token(self):
        with patch("update_star_history.urlopen", return_value=Response([], '<https://example.com/>; rel="next"')) as request:
            with self.assertRaises(ValueError):
                list(GitHub("secret").pages(f"/users/{OWNER}/repos"))
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
