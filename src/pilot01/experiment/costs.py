"""Versioned reference estimates, never provider invoices or agent inputs."""
from datetime import date, datetime, timedelta, timezone

PRICE_VERSION = "deepseek-official-usd-2026-10-19"
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
CALENDAR_SOURCE = "https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html"
HOLIDAYS_2026 = set()
for start, end in (("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
                   ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"), ("10-01", "10-07")):
    first, last = date.fromisoformat("2026-" + start), date.fromisoformat("2026-" + end)
    HOLIDAYS_2026.update(first + timedelta(days=i) for i in range((last-first).days+1))
# Snapshot verified on 2026-10-19; USD per million tokens, peak rates.
MODELS = {
    "flash": dict(cached=0.006, input=0.30, output=1.20),
    "pro": dict(cached=0.044, input=1.32, output=3.96),
}
ALIASES = {name: "flash" for name in (
    "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
    "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017")}
ALIASES.update({"deepseek-v4-pro": "pro", "deepseek-v4-pro-0813": "pro"})


def estimate_call(call):
    usage = call.get("usage") or {}
    model = call.get("response_model_id") or (call.get("params") or {}).get("model_id", "")
    family = ALIASES.get(model.lower())
    result = dict(label="按 DeepSeek 官方价估算", currency="USD", price_version=PRICE_VERSION,
                  source=PRICE_SOURCE, model_id=model, amount=None, reason=None)
    if not family:
        return dict(result, reason="unknown_model_price")
    try:
        at = datetime.fromisoformat(call["requested_at"].replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError("timezone absent")
        at = at.astimezone(timezone.utc)
    except (KeyError, ValueError, AttributeError, TypeError):
        return dict(result, reason="missing_call_time")
    # Saturday/Sunday are always off-peak. Weekdays need a verified holiday
    # calendar: do not silently classify a Chinese public holiday as peak.
    in_window = at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)
    if in_window and at.year != 2026:
        return dict(result, reason="holiday_calendar_required",
                    peak_amount_if_workday=_amount(usage, MODELS[family]),
                    off_peak_amount_if_holiday=_amount(usage, {k:v / 2 for k,v in MODELS[family].items()}))
    peak = in_window and at.date() not in HOLIDAYS_2026
    rates = {k:v / (1 if peak else 2) for k,v in MODELS[family].items()}
    amount = _amount(usage, rates)
    return dict(result, amount=amount, reason=None if amount is not None else "missing_or_invalid_usage",
                period="peak" if peak else "off_peak", rates_per_million=rates,
                calendar_version="CN-2026-State-Council-7", calendar_source=CALENDAR_SOURCE,
                input_tokens=usage.get("prompt_tokens"), cached_tokens=usage.get("cached_tokens"),
                output_tokens=usage.get("completion_tokens"))


def _amount(usage, rates):
    p, c, o = (usage.get(k) for k in ("prompt_tokens", "cached_tokens", "completion_tokens"))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in (p,c,o)) or c > p:
        return None
    return ((p - c) * rates["input"] + c * rates["cached"] + o * rates["output"]) / 1_000_000


def total_cost(calls):
    estimates = [estimate_call(c) for c in calls]
    known = [c["amount"] for c in estimates if c["amount"] is not None]
    return dict(label="按 DeepSeek 官方价估算", currency="USD", price_version=PRICE_VERSION,
                source=PRICE_SOURCE, amount=sum(known) if known and len(known) == len(calls) else None,
                known_partial_amount=sum(known), priced_calls=len(known), total_calls=len(calls),
                missing_calls=len(calls) - len(known),
                note="第三方接口的官方价参考成本；reasoning_tokens 已包含在输出时不重复计费。")
