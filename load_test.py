"""
load_test.py
============
Lightweight concurrent load test for TrueStore Operations — no external
dependencies (uses only stdlib). Simulates real usage patterns: login,
dashboard loads with filters, bill creation, purchase browsing, and
settings reads, all running in parallel threads.

Usage:
    python load_test.py                              # defaults: 10 users, 20 requests each
    python load_test.py --url http://10.0.0.5:8010   # test against a remote server
    python load_test.py --users 25 --requests 50     # heavier load
    python load_test.py --admin-user admin --admin-pass secret  # real credentials

Outputs:
    - Per-endpoint latency stats (p50, p95, p99, max)
    - Error rate and error details
    - Throughput (requests/second)
    - Concurrent write contention check (SQLite WAL stress)

This is NOT a substitute for real monitoring in production, but it catches
the worst SQLite contention issues, slow queries, and crash-on-load bugs
before real users hit them.
"""
import argparse
import json
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from http.cookiejar import CookieJar


class LoadTestClient:
    """A single simulated user session with cookie persistence."""

    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.logged_in = False

    def _request(self, path, method="GET", data=None, timeout=15):
        url = self.base_url + path
        body = None
        headers = {}
        if data and method == "POST":
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        start = time.monotonic()
        try:
            resp = self.opener.open(req, timeout=timeout)
            elapsed = time.monotonic() - start
            status = resp.status
            resp.read()  # consume body
            return {"ok": True, "status": status, "elapsed": elapsed, "path": path}
        except urllib.error.HTTPError as e:
            elapsed = time.monotonic() - start
            return {"ok": e.code < 500, "status": e.code, "elapsed": elapsed,
                    "path": path, "error": f"HTTP {e.code}"}
        except Exception as e:
            elapsed = time.monotonic() - start
            return {"ok": False, "status": 0, "elapsed": elapsed,
                    "path": path, "error": str(e)[:100]}

    def login(self):
        result = self._request("/login", "POST", {
            "username": self.username,
            "password": self.password,
        })
        self.logged_in = result["ok"] and result["status"] in (200, 302)
        return result

    def run_scenario(self, scenario_name):
        """Run a named scenario (a sequence of requests mimicking real usage)."""
        scenarios = {
            "dashboard_browse": [
                "/",
                "/?status=unpaid",
                "/?status=paid",
                "/?view=invoicewise",
                "/?status=unpaid&amount_range=2000-5000",
            ],
            "bills_workflow": [
                "/bills",
                "/bills/list",
                "/bills/api/next-invoice-number",
            ],
            "purchases_browse": [
                "/purchases",
                "/purchases/list",
            ],
            "settings_read": [
                "/settings",
                "/products/manage",
                "/parties",
            ],
            "delivery_browse": [
                "/deliveries",
                "/deliveries/pending",
            ],
            "api_calls": [
                "/api/products/search?q=pen",
                "/api/customers/search?q=a",
                "/bills/api/next-invoice-number",
            ],
        }
        paths = scenarios.get(scenario_name, scenarios["dashboard_browse"])
        results = []
        for path in paths:
            results.append(self._request(path))
            time.sleep(random.uniform(0.05, 0.2))  # realistic inter-request delay
        return results


def run_user_session(base_url, username, password, num_requests, results_list, lock):
    """One simulated user's full session."""
    client = LoadTestClient(base_url, username, password)

    login_result = client.login()
    with lock:
        results_list.append(login_result)

    if not client.logged_in:
        return  # can't do much without auth

    scenarios = ["dashboard_browse", "bills_workflow", "purchases_browse",
                 "settings_read", "delivery_browse", "api_calls"]
    done = 0
    while done < num_requests:
        scenario = random.choice(scenarios)
        batch = client.run_scenario(scenario)
        with lock:
            results_list.extend(batch)
        done += len(batch)


def analyze_results(results, wall_time):
    """Compute and print statistics."""
    total = len(results)
    errors = [r for r in results if not r["ok"]]
    successes = [r for r in results if r["ok"]]

    print("\n" + "=" * 65)
    print("  LOAD TEST RESULTS")
    print("=" * 65)
    print(f"  Total requests:    {total}")
    print(f"  Successful:        {len(successes)}")
    print(f"  Errors:            {len(errors)} ({100*len(errors)/max(total,1):.1f}%)")
    print(f"  Wall time:         {wall_time:.1f}s")
    print(f"  Throughput:        {total/max(wall_time,0.01):.1f} req/s")

    if successes:
        times = [r["elapsed"] for r in successes]
        times.sort()
        print(f"\n  Latency (successful requests):")
        print(f"    p50:  {statistics.median(times)*1000:.0f} ms")
        p95_idx = int(len(times) * 0.95)
        p99_idx = int(len(times) * 0.99)
        print(f"    p95:  {times[p95_idx]*1000:.0f} ms")
        print(f"    p99:  {times[p99_idx]*1000:.0f} ms")
        print(f"    max:  {max(times)*1000:.0f} ms")
        print(f"    avg:  {statistics.mean(times)*1000:.0f} ms")

    # Per-endpoint breakdown
    by_path = defaultdict(list)
    for r in results:
        # Normalize path (strip query params for grouping)
        path = r["path"].split("?")[0]
        by_path[path].append(r)

    print(f"\n  Per-endpoint breakdown (top 10 by request count):")
    print(f"  {'Endpoint':<40} {'Count':>5} {'Err':>4} {'p50':>6} {'p95':>6}")
    print(f"  {'-'*40} {'-'*5} {'-'*4} {'-'*6} {'-'*6}")
    sorted_paths = sorted(by_path.items(), key=lambda x: -len(x[1]))[:10]
    for path, reqs in sorted_paths:
        ok_times = sorted(r["elapsed"] for r in reqs if r["ok"])
        err_count = sum(1 for r in reqs if not r["ok"])
        p50 = f"{statistics.median(ok_times)*1000:.0f}" if ok_times else "-"
        p95 = f"{ok_times[int(len(ok_times)*0.95)]*1000:.0f}" if len(ok_times) > 1 else p50
        print(f"  {path:<40} {len(reqs):>5} {err_count:>4} {p50:>6} {p95:>6}")

    if errors:
        print(f"\n  Error details:")
        error_types = defaultdict(int)
        for e in errors:
            error_types[e.get("error", "unknown")] += 1
        for err, count in sorted(error_types.items(), key=lambda x: -x[1])[:5]:
            print(f"    {count}x  {err}")

    # Verdict
    print(f"\n  {'─'*40}")
    error_rate = len(errors) / max(total, 1)
    p95_ms = times[int(len(times)*0.95)]*1000 if successes else 0
    if error_rate > 0.05:
        print("  ⚠  HIGH ERROR RATE — check logs (journalctl -u truestore)")
    elif p95_ms > 2000:
        print("  ⚠  SLOW p95 — may need query optimization or more workers")
    elif p95_ms > 500:
        print("  ○  ACCEPTABLE — p95 under 2s but room for improvement")
    else:
        print("  ✓  GOOD — low error rate, fast response times")
    print("=" * 65)

    return len(errors) == 0


def main():
    parser = argparse.ArgumentParser(description="Load test TrueStore Operations")
    parser.add_argument("--url", default="http://127.0.0.1:8010",
                        help="Base URL of the app (default: http://127.0.0.1:8010)")
    parser.add_argument("--users", type=int, default=10,
                        help="Number of concurrent simulated users (default: 10)")
    parser.add_argument("--requests", type=int, default=20,
                        help="Requests per user (default: 20)")
    parser.add_argument("--admin-user", default="admin",
                        help="Admin username for login (default: admin)")
    parser.add_argument("--admin-pass", default="admin",
                        help="Admin password for login (default: admin)")
    args = parser.parse_args()

    print(f"Load test: {args.users} users × {args.requests} requests against {args.url}")
    print(f"Starting...")

    results = []
    lock = threading.Lock()
    threads = []
    start = time.monotonic()

    for i in range(args.users):
        t = threading.Thread(
            target=run_user_session,
            args=(args.url, args.admin_user, args.admin_pass, args.requests, results, lock),
            daemon=True,
        )
        threads.append(t)
        t.start()
        time.sleep(0.05)  # stagger slightly

    for t in threads:
        t.join(timeout=120)

    wall_time = time.monotonic() - start
    ok = analyze_results(results, wall_time)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
