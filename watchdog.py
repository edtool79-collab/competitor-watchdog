#!/usr/bin/env python3
"""Competitor Watchdog: new Google ads and job changes of your competitors, once a day.

Calls two Apify Actors with YOUR Apify token and posts a short summary:
  - hirepulse/google-ads-transparency-scraper  (only new ads since your last run)
  - hirepulse/ats-job-pulse                    (jobs opened or closed since your last run)

Standard library only. Python 3.10+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.apify.com/v2"
ADS_ACTOR = "hirepulse~google-ads-transparency-scraper"
JOBS_ACTOR = "hirepulse~ats-job-pulse"

# Prices from the Actors' published pricing. The jobs Actor has no price here: see its Apify page.
ADS_PER_DOMAIN_USD = 0.005
ADS_PER_AD_USD = 0.0015

MAX_DOMAINS = 25
MAX_COMPANIES = 25
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$|^(greenhouse|lever|ashby|workable|recruitee|smartrecruiters):[A-Za-z0-9._-]{1,64}$")
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


class ConfigError(Exception):
    pass


def load_config(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    ads = raw.get("ads", {})
    jobs = raw.get("jobs", {})
    cfg = {
        "ads_domains": [d.strip().lower() for d in ads.get("domains", [])],
        "ads_max_per_domain": int(ads.get("max_ads_per_domain", 20)),
        "ads_country": (ads.get("country") or "").strip().upper(),
        "ads_cap": float(ads.get("max_charge_usd", 0.5)),
        "companies": [c.strip() for c in jobs.get("companies", [])],
        "jobs_state_id": jobs.get("state_id", "competitor-watchdog"),
        "jobs_title_keywords": list(jobs.get("title_keywords", [])),
        "jobs_cap": float(jobs.get("max_charge_usd", 0.5)),
        "total_cap": float(raw.get("max_total_usd_per_run", 1.0)),
    }
    if not cfg["ads_domains"] and not cfg["companies"]:
        raise ConfigError("Add at least one domain under ads.domains or one company under jobs.companies.")
    if len(cfg["ads_domains"]) > MAX_DOMAINS or len(cfg["companies"]) > MAX_COMPANIES:
        raise ConfigError(f"At most {MAX_DOMAINS} ad domains and {MAX_COMPANIES} companies per run.")
    for domain in cfg["ads_domains"]:
        if not HOST_RE.match(domain):
            raise ConfigError(f"'{domain}' is not a plain domain such as nike.com.")
    for company in cfg["companies"]:
        if not SLUG_RE.match(company.lower() if ":" not in company else company):
            raise ConfigError(f"'{company}' is not a domain or board slug such as stripe.com or greenhouse:stripe.")
    if not 1 <= cfg["ads_max_per_domain"] <= 120:
        raise ConfigError("ads.max_ads_per_domain must be between 1 and 120.")
    if cfg["ads_country"] and not re.fullmatch(r"[A-Z]{2}", cfg["ads_country"]):
        raise ConfigError("ads.country must be a two-letter code such as US, or empty.")
    for name in ("ads_cap", "jobs_cap", "total_cap"):
        if not 0 < cfg[name] <= 100:
            raise ConfigError("Every cost cap must be above 0 and at most 100 USD.")
    active = (cfg["ads_cap"] if cfg["ads_domains"] else 0) + (cfg["jobs_cap"] if cfg["companies"] else 0)
    if active > cfg["total_cap"] + 1e-9:
        raise ConfigError(
            f"The caps of the Actors add up to ${active:.2f}, above max_total_usd_per_run (${cfg['total_cap']:.2f}). "
            "Lower the caps or raise the total on purpose."
        )
    return cfg


def worst_case_ads_usd(cfg: dict) -> float:
    n = len(cfg["ads_domains"])
    return n * ADS_PER_DOMAIN_USD + n * cfg["ads_max_per_domain"] * ADS_PER_AD_USD


def plan_text(cfg: dict) -> str:
    lines = ["Plan (nothing has been called yet):"]
    if cfg["ads_domains"]:
        lines.append(f"- Ads: {len(cfg['ads_domains'])} domain(s), only new ads, up to {cfg['ads_max_per_domain']} per domain. "
                     f"Run cap ${cfg['ads_cap']:.2f}. Largest possible price by the published rates (the first run, when every ad is new): "
                     f"${worst_case_ads_usd(cfg):.2f}.")
    if cfg["companies"]:
        lines.append(f"- Jobs: {len(cfg['companies'])} company(ies), only changes. Run cap ${cfg['jobs_cap']:.2f}. "
                     "The price of this Actor is on its Apify page.")
    lines.append(f"- Total cap for the run: ${cfg['total_cap']:.2f}. The caps are enforced by Apify per run.")
    return "\n".join(lines)


def api(method: str, path: str, token: str, body: dict | None = None, params: dict | None = None):
    """One call to the Apify API. The token only travels in the Authorization header and is never printed."""
    url = API + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "competitor-watchdog/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(text).get("error", {}).get("message", "")
        except ValueError:
            message = ""
        raise RuntimeError(f"Apify answered HTTP {exc.code} for {method} {path}. {message[:200]}") from None


def run_actor(token: str, actor: str, run_input: dict, cap_usd: float, timeout_s: int = 1200) -> list[dict]:
    """Start one run with a cost cap, wait for it, return its dataset items. Never retries (every retry would cost money)."""
    started = api("POST", f"/acts/{actor}/runs", token, run_input, {"maxTotalChargeUsd": cap_usd, "waitForFinish": 60})
    run = started["data"]
    deadline = time.time() + timeout_s
    while run["status"] not in TERMINAL:
        if time.time() > deadline:
            raise RuntimeError(f"{actor}: still running after {timeout_s // 60} minutes. Check the run in the Apify console.")
        run = api("GET", f"/actor-runs/{run['id']}", token, params={"waitForFinish": 60})["data"]
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"{actor}: run ended as {run['status']}. Open run {run['id']} in the Apify console.")
    items = api("GET", f"/datasets/{run['defaultDatasetId']}/items", token, params={"clean": "true", "format": "json"})
    return items if isinstance(items, list) else []


def ads_input(cfg: dict) -> dict:
    run_input = {"domains": cfg["ads_domains"], "mode": "ads", "max_ads_per_domain": cfg["ads_max_per_domain"], "only_new_ads": True}
    if cfg["ads_country"]:
        run_input["country"] = cfg["ads_country"]
    return run_input


def jobs_input(cfg: dict) -> dict:
    run_input = {"companies": cfg["companies"], "output_mode": "jobs_and_summary", "track_diff": True,
                 "state_id": cfg["jobs_state_id"], "only_changes": True}
    if cfg["jobs_title_keywords"]:
        run_input["title_keywords"] = cfg["jobs_title_keywords"]
    return run_input


def format_ads(items: list[dict]) -> list[str]:
    ads = [i for i in items if i.get("ad_id")]
    if not ads:
        return ["Ads: no new ads."]
    by_domain: dict[str, list[dict]] = {}
    for ad in ads:
        by_domain.setdefault(ad.get("domain", "?"), []).append(ad)
    lines = [f"Ads: {len(ads)} new ad(s)."]
    for domain, group in by_domain.items():
        lines.append(f"*{domain}*: {len(group)} new")
        for ad in group[:10]:
            when = (ad.get("first_shown") or "")[:10]
            lines.append(f"  - {ad.get('advertiser_name', 'unknown advertiser')}, {ad.get('format', '?')}, first shown {when}: {ad.get('transparency_url', '')}")
        if len(group) > 10:
            lines.append(f"  - and {len(group) - 10} more")
    return lines


def format_jobs(items: list[dict]) -> list[str]:
    summaries = [i for i in items if i.get("record_type") == "company_summary"]
    jobs = [i for i in items if i.get("record_type") == "job"]
    lines = []
    for s in summaries:
        domain = s.get("company_domain", "?")
        if s.get("is_first_run"):
            lines.append(f"Jobs, *{domain}*: first run, {s.get('total_active_jobs', '?')} open jobs saved as the starting point. Changes appear from the next run.")
            continue
        new, closed = s.get("new_jobs") or 0, s.get("closed_jobs") or 0
        if not new and not closed:
            continue
        lines.append(f"Jobs, *{domain}*: {new} new, {closed} closed (open now: {s.get('total_active_jobs', '?')})")
        shown = [j for j in jobs if j.get("company_domain") == domain and j.get("status") == "NEW"][:10]
        for job in shown:
            where = f", {job['location']}" if job.get("location") else ""
            lines.append(f"  - NEW: {job.get('title', '?')}{where}: {job.get('apply_url', '')}")
        for title in (s.get("closed_job_titles") or [])[:5]:
            lines.append(f"  - closed: {title}")
    return lines or ["Jobs: no changes."]


def post_slack(webhook: str, text: str) -> None:
    if not webhook.startswith("https://hooks.slack.com/"):
        raise RuntimeError("SLACK_WEBHOOK_URL must start with https://hooks.slack.com/")
    request = urllib.request.Request(webhook, data=json.dumps({"text": text[:3500]}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily competitor ads and jobs summary through Apify.")
    parser.add_argument("--config", default="watchdog.config.json")
    parser.add_argument("--dry-run", action="store_true", help="validate the config and print the plan; call nothing, spend nothing")
    parser.add_argument("--baseline", action="store_true", help="run, but do not post: use it once so the first run does not flood the channel")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"Config problem: {exc}", file=sys.stderr)
        return 2
    print(plan_text(cfg))
    if args.dry_run:
        return 0

    token = os.environ.get("APIFY_TOKEN", "")
    if not token:
        print("APIFY_TOKEN is not set. In GitHub: Settings, Secrets and variables, Actions, New repository secret.", file=sys.stderr)
        return 2

    lines, failed = [], False
    for name, enabled, actor, run_input, cap, fmt in (
        ("ads", bool(cfg["ads_domains"]), ADS_ACTOR, ads_input(cfg), cfg["ads_cap"], format_ads),
        ("jobs", bool(cfg["companies"]), JOBS_ACTOR, jobs_input(cfg), cfg["jobs_cap"], format_jobs),
    ):
        if not enabled:
            continue
        try:
            lines += fmt(run_actor(token, actor, run_input, cap))
        except RuntimeError as exc:
            failed = True
            lines.append(f"{name}: FAILED. {exc}")
    text = "Competitor Watchdog\n" + "\n".join(lines)
    print(text)

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write("```\n" + text + "\n```\n")
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    if webhook and not args.baseline:
        try:
            post_slack(webhook, text)
        except (RuntimeError, urllib.error.URLError) as exc:
            print(f"Could not post to Slack: {type(exc).__name__}", file=sys.stderr)
            failed = True
    elif args.baseline:
        print("Baseline run: nothing was posted.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
