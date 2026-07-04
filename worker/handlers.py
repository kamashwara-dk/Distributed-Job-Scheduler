# Built-in job handlers. A handler is fn(payload, ctx) -> dict | None.
# ctx.log(msg) writes to the job's log stream; ctx.attempt is 1-based.
#
# Real deployments would register their own handlers here (send actual emails,
# call actual services); these built-ins simulate work and give the demo
# controllable failures to show off retries and the DLQ.

import random
import time

import httpx


class HandlerContext:
    def __init__(self, attempt: int, timeout_s: int | None, log_fn):
        self.attempt = attempt
        self.timeout_s = timeout_s
        self._log = log_fn

    def log(self, message: str, level: str = "info"):
        self._log(message, level)


def sleep_handler(payload: dict, ctx: HandlerContext):
    seconds = float(payload.get("seconds", 1))
    ctx.log(f"sleeping {seconds}s (simulated work)")
    time.sleep(seconds)
    return {"slept_s": seconds}


def send_email(payload: dict, ctx: HandlerContext):
    to = payload.get("to", "user@example.com")
    subject = payload.get("subject", "(no subject)")
    ctx.log(f"rendering template for {to}")
    time.sleep(random.uniform(0.2, 0.8))
    ctx.log(f"SMTP handshake ok, sending '{subject}'")
    time.sleep(random.uniform(0.2, 0.6))
    ctx.log("delivered")
    return {"to": to, "subject": subject, "delivered": True}


def http_request(payload: dict, ctx: HandlerContext):
    url = payload["url"]
    method = payload.get("method", "GET").upper()
    ctx.log(f"{method} {url}")
    resp = httpx.request(method, url, json=payload.get("json"),
                         timeout=ctx.timeout_s or 30)
    resp.raise_for_status()
    ctx.log(f"response {resp.status_code} ({len(resp.content)} bytes)")
    return {"status_code": resp.status_code}


def generate_report(payload: dict, ctx: HandlerContext):
    name = payload.get("name", "report")
    rows = int(payload.get("rows", 5000))
    for pct in (10, 40, 70, 90):
        ctx.log(f"{name}: processed {rows * pct // 100}/{rows} rows ({pct}%)")
        time.sleep(random.uniform(0.3, 0.7))
    ctx.log(f"{name}: rendered PDF")
    return {"report": name, "rows": rows}


def flaky(payload: dict, ctx: HandlerContext):
    """Fails the first `fail_times` attempts, then succeeds — the retry demo."""
    fail_times = int(payload.get("fail_times", 2))
    if ctx.attempt <= fail_times:
        ctx.log(f"attempt {ctx.attempt}/{fail_times} destined to fail", level="warn")
        time.sleep(0.3)
        raise RuntimeError(f"flaky failure {ctx.attempt}/{fail_times} (simulated)")
    time.sleep(0.3)
    ctx.log(f"succeeded on attempt {ctx.attempt}")
    return {"succeeded_on_attempt": ctx.attempt}


def always_fail(payload: dict, ctx: HandlerContext):
    """Never succeeds — exercises max_attempts -> dead letter queue."""
    time.sleep(0.2)
    raise RuntimeError(payload.get("error", "simulated permanent failure"))


HANDLERS = {
    "sleep": sleep_handler,
    "send_email": send_email,
    "http_request": http_request,
    "generate_report": generate_report,
    "flaky": flaky,
    "always_fail": always_fail,
}
