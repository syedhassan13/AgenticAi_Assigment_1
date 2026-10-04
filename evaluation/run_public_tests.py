"""Public test runner: sends HTTP requests to the running agent.

Usage:
  python evaluation/run_public_tests.py --url http://127.0.0.1:8000

WARNING: After implementing a model, these may incur provider cost.
Results are saved to evaluation/public_test_results.json.
"""

import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description="Run public Arena test cases")
    parser.add_argument('--url', default='http://127.0.0.1:8000',
                        help='Base URL of the agent')
    parser.add_argument('--timeout', type=int, default=70,
                        help='HTTP timeout per request in seconds')
    args = parser.parse_args()

    base_url = args.url.rstrip('/')
    cases_path = Path(__file__).with_name('public_cases.json')
    cases = json.loads(cases_path.read_text())

    # First check health
    try:
        health = httpx.get(f'{base_url}/health', timeout=10)
        health.raise_for_status()
        print(f"Health: {health.json()}")
    except Exception as e:
        print(f"WARN: Health check failed: {e}")

    results = []
    passed = 0
    total = len(cases)

    print(f"\nRunning {total} public test cases against {base_url}\n")
    print("-" * 70)

    for i, case in enumerate(cases, 1):
        name = case['name']
        category = case.get('category', '?')

        start = time.time()
        try:
            response = httpx.post(
                f'{base_url}/arena/run',
                json=case['request'],
                timeout=args.timeout
            )
            response.raise_for_status()
            data = response.json()
            elapsed = time.time() - start

            # Check pass conditions
            status = data.get('status', '')
            stop_reason = data.get('stop_reason', '')

            if 'expected_status' in case:
                ok = (status == case['expected_status'])
                if 'expected_stop_reason' in case:
                    ok = ok and (stop_reason == case['expected_stop_reason'])
            elif 'allow_statuses' in case:
                ok = status in case['allow_statuses']
            else:
                ok = True  # No expected value, just check it returned

            # Schema validity check
            schema_valid = all(k in data for k in [
                'status', 'final_response', 'steps', 'stop_reason'
            ])

            if ok and schema_valid:
                passed += 1
                verdict = "PASS"
            else:
                verdict = "FAIL"

            result_entry = {
                "name": name,
                "category": category,
                "verdict": verdict,
                "status": status,
                "stop_reason": stop_reason,
                "steps": data.get('steps', 0),
                "schema_valid": schema_valid,
                "latency_s": round(elapsed, 2),
                "final_response_preview": data.get('final_response', '')[:100],
                "errors": data.get('errors', []),
            }
            results.append(result_entry)

            print(f"  [{verdict}] {name}")
            print(f"        status={status}  stop_reason={stop_reason}  "
                  f"steps={data.get('steps', 0)}  latency={elapsed:.1f}s")

        except Exception as exc:
            elapsed = time.time() - start
            results.append({
                "name": name,
                "category": category,
                "verdict": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
                "latency_s": round(elapsed, 2),
            })
            print(f"  [FAIL] {name} -- {type(exc).__name__}: {exc}")

    print("-" * 70)
    print(f"\n{passed}/{total} checks passed.")
    print("Note: These scaffold checks are not an Arena score.\n")

    # Save results
    results_path = Path(__file__).parent / 'public_test_results.json'
    results_path.write_text(json.dumps({
        "url": base_url,
        "total": total,
        "passed": passed,
        "results": results,
    }, indent=2))
    print(f"Results saved to {results_path}")

    raise SystemExit(0 if passed == total else 1)


if __name__ == '__main__':
    main()
