#!/usr/bin/env python3
"""End-to-end test for the shipped workflow (no pytest, no network, no API keys).

    python3 tests/run_tests.py              # all tests
    python3 tests/run_tests.py -k redact    # only tests whose name matches
    python3 tests/run_tests.py --keep       # leave the containers up for debugging

One pinned n8n container imports the workflow JSON exactly as it ships (only the Settings node is
overlaid, so the LLM endpoint and the chat webhook point at the mock container) and the tests drive
it through its real webhook, asserting on what n8n actually sent upstream.
"""
import argparse
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

import payloads  # noqa: E402
from stack import Stack, TestFailure, sweep  # noqa: E402

MOCK = "http://mock:8080"
OVERLAY = {
    "llm": {"url": f"{MOCK}/v1/chat/completions", "model": "mock-model-1", "timeout_seconds": 20},
    "notify": {"url": f"{MOCK}/slack/T000/B000/e2e", "format": "slack"},
    "links": {"alertmanager_url": "http://alertmanager.test:9093"},
}


def assert_in(needle, haystack, what):
    if needle not in haystack:
        raise TestFailure(f"{what}: expected {needle!r} in:\n{haystack[:1500]}")


def assert_not_in(needle, haystack, what):
    if needle in haystack:
        raise TestFailure(f"{what}: did NOT expect {needle!r} in:\n{haystack[:1500]}")


def chat_text(entry):
    return (entry["json"] or {}).get("text", "")


# --------------------------------------------------------------------------- tests

def test_firing_group_is_triaged_and_posted(st):
    """A firing group reaches the LLM and one chat message is posted with the AI summary."""
    st.triage(payloads.alertmanager([payloads.alert(
        "HighErrorRate", "critical",
        labels={"service": "checkout", "namespace": "shop"},
        annotations={"summary": "5xx rate above SLO", "runbook_url": "https://runbooks.example.com/HighErrorRate"})]))
    llm = st.wait_for("/v1/chat/completions")[0]
    assert_in("Bearer ", llm["headers"].get("authorization", ""), "LLM call is authenticated")
    assert_in("HighErrorRate", llm["body"], "prompt carries the alert")
    assert_in("untrusted data", llm["body"], "prompt marks alert data as untrusted")
    if llm["json"]["model"] != "mock-model-1":
        raise TestFailure(f"model from Settings not used: {llm['json']['model']}")

    msg = chat_text(st.wait_for("/slack/")[0])
    assert_in("[FIRING:1] HighErrorRate", msg, "title")
    assert_in("MOCK summary", msg, "AI summary in the message")
    assert_in("MOCK likely cause", msg, "likely cause in the message")
    assert_in("First checks", msg, "first checks in the message")
    assert_in("https://runbooks.example.com/HighErrorRate", msg, "runbook link")
    assert_in("Silence", msg, "silence link")
    assert_in("mock-model-1", msg, "model disclosure")


def test_resolved_group_skips_the_llm(st):
    """A resolved group must not spend a token: short message, no LLM call."""
    st.triage(payloads.alertmanager([payloads.alert("HighErrorRate", "critical", status="resolved")]))
    msg = chat_text(st.wait_for("/slack/")[0])
    assert_in("[RESOLVED:1] HighErrorRate", msg, "resolved title")
    time.sleep(2)
    if st.log("/v1/chat/completions"):
        raise TestFailure("resolved group called the LLM")


def test_secrets_are_redacted_before_the_llm_and_chat(st):
    """Secret-looking label values and tokens in text never leave n8n."""
    st.triage(payloads.alertmanager([payloads.alert(
        "DatabaseDown", "critical",
        labels={"service": "db", "password": "hunter2-super-secret"},
        annotations={"summary": "connect failed with Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz012345",
                     "description": "contact oncall@example.com"})]))
    llm = st.wait_for("/v1/chat/completions")[0]
    assert_not_in("hunter2-super-secret", llm["body"], "secret label value sent to the LLM")
    assert_not_in("ghp_abcdefghijklmnopqrstuvwxyz012345", llm["body"], "token sent to the LLM")
    assert_not_in("oncall@example.com", llm["body"], "email sent to the LLM")
    assert_in("[REDACTED]", llm["body"], "redaction marker present")

    msg = chat_text(st.wait_for("/slack/")[0])
    assert_not_in("hunter2-super-secret", msg, "secret label value posted to chat")
    assert_not_in("ghp_abcdefghijklmnopqrstuvwxyz012345", msg, "token posted to chat")


def test_llm_failure_still_notifies(st):
    """When the LLM answers 500 the on-call engineer still gets the alert."""
    st.triage(payloads.alertmanager([payloads.alert(
        "DiskWillFillUp", "warning", annotations={"summary": "MockLlm500 disk 92% used"})]))
    msg = chat_text(st.wait_for("/slack/")[0])
    assert_in("AI summary unavailable", msg, "degraded note")
    assert_in("DiskWillFillUp", msg, "alert still listed")


def test_malformed_llm_answer_is_not_posted_as_a_summary(st):
    """Prose instead of JSON degrades to the same honest note."""
    st.triage(payloads.alertmanager([payloads.alert(
        "TargetDown", "warning", annotations={"summary": "MockLlmMalformed scrape failed"})]))
    msg = chat_text(st.wait_for("/slack/")[0])
    assert_in("did not return the expected JSON", msg, "malformed answer handled")
    assert_not_in("likely_cause", msg, "raw model text leaked into the message")


def test_fenced_json_answer_is_parsed(st):
    """Models that wrap JSON in a ```json fence are still understood."""
    st.triage(payloads.alertmanager([payloads.alert(
        "CertExpiringSoon", "warning", annotations={"summary": "MockLlmFenced cert expires in 5 days"})]))
    msg = chat_text(st.wait_for("/slack/")[0])
    assert_in("MOCK summary", msg, "fenced JSON parsed")
    assert_not_in("```", msg, "code fence leaked into the message")


def test_group_of_alerts_is_one_message_at_the_highest_severity(st):
    """Alertmanager groups arrive as one payload and must produce exactly one message."""
    st.triage(payloads.alertmanager([
        payloads.alert("KubePodCrashLooping", "warning", labels={"namespace": "shop", "pod": "api-1"}),
        payloads.alert("KubePodCrashLooping", "warning", labels={"namespace": "shop", "pod": "api-2"}),
        payloads.alert("KubeContainerOOMKilled", "critical", labels={"namespace": "shop", "pod": "api-3"}),
    ]))
    msg = chat_text(st.wait_for("/slack/")[0])
    assert_in("[FIRING:3]", msg, "all three alerts counted")
    assert_in(":rotating_light:", msg, "highest severity (critical) icon")
    assert_in("api-3", msg, "third alert listed")
    time.sleep(2)
    if len(st.log("/slack/")) != 1:
        raise TestFailure(f"expected exactly one chat message, got {len(st.log('/slack/'))}")


def test_shipped_sample_payload_produces_a_message(st):
    """The payload in examples/samples/ is the one people try first: it must work as shipped."""
    sample = os.path.join(os.path.dirname(HERE), "examples", "samples", "alertmanager-firing.json")
    st.triage(json.load(open(sample)))
    msg = chat_text(st.wait_for("/slack/")[0])
    assert_in("[FIRING:2] HighErrorRate, HighLatency", msg, "sample group title")
    assert_in("MOCK summary", msg, "sample group triaged")


def test_webhook_requires_the_header_credential(st):
    """An unauthenticated POST is rejected by n8n before anything runs."""
    status, _ = st.webhook("ftw-alertmanager-triage",
                           payloads.alertmanager([payloads.alert("Anything")]), auth=None)
    if status not in (401, 403):
        raise TestFailure(f"unauthenticated webhook returned {status}, expected 401/403")
    status, _ = st.webhook("ftw-alertmanager-triage",
                           payloads.alertmanager([payloads.alert("Anything")]), auth="Bearer wrong")
    if status not in (401, 403):
        raise TestFailure(f"wrong credential returned {status}, expected 401/403")
    time.sleep(2)
    if st.log("/v1/chat/completions") or st.log("/slack/"):
        raise TestFailure("a rejected request still reached an upstream")


def test_non_alertmanager_payload_fails_loudly(st):
    """A misconfigured receiver must not post a half-empty message."""
    st.webhook("ftw-alertmanager-triage", {"hello": "world"})
    time.sleep(3)
    if st.log("/slack/"):
        raise TestFailure("a payload without alerts[] still produced a chat message")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", action="append", default=[], help="only tests whose name contains this")
    ap.add_argument("--keep", action="store_true", help="leave the containers running")
    args = ap.parse_args()

    tests = [t for t in TESTS if not args.k or any(f in t.__name__ for f in args.k)]
    results = []
    t0 = time.time()
    st = Stack(settings_overlay=OVERLAY)
    try:
        st.start()
        print(f"stack ready in {time.time() - t0:.0f}s ({st.base})", flush=True)
        for fn in tests:
            st.reset_log()
            ts = time.time()
            try:
                fn(st)
                results.append((fn.__name__, True))
                print(f"PASS  {fn.__name__}  ({time.time() - ts:.1f}s)", flush=True)
            except Exception:  # noqa: BLE001
                results.append((fn.__name__, False))
                print(f"FAIL  {fn.__name__}  ({time.time() - ts:.1f}s)\n{traceback.format_exc()}", flush=True)
    except Exception:  # noqa: BLE001
        print(f"FAIL  stack\n{traceback.format_exc()}", flush=True)
        print(st.logs(80), flush=True)
        results.append(("stack", False))
    finally:
        if args.keep:
            print(f"keeping containers: {st.n8n} {st.mock} ({st.base})", flush=True)
        else:
            st.close()
            sweep()

    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} tests passed in {time.time() - t0:.0f}s")
    for name, ok in results:
        if not ok:
            print(f"  failed: {name}")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
