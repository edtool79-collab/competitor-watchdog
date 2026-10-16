# Competitor Watchdog

A small script that runs every morning in your own GitHub repository and tells you two things about the competitors you list:

- **New Google ads** they started running (from the Google Ads Transparency Center).
- **Jobs they opened or closed** (from their public job boards: Greenhouse, Lever, Ashby, Workable, Recruitee, SmartRecruiters).

It posts a short summary to Slack and to the run's summary page in GitHub. It uses two pay-per-use Actors on the Apify Store, **with your own Apify account and token**. I make those two Actors, so this script is also an example of how to call them.

- `hirepulse/google-ads-transparency-scraper`
- `hirepulse/ats-job-pulse`

## What it costs, and how it is limited

You pay Apify directly, per result. Nothing runs outside your account.

- **Ads:** $0.005 per domain checked + $0.0015 per ad returned. The first run returns the most recent ads of every domain, so it is the most expensive. With 10 domains and 20 ads per domain, the most it can cost by these rates is 10 x $0.005 + 10 x 20 x $0.0015 = $0.35. Later runs return only ads that are new. A domain that has no ads costs nothing, and a domain that has ads is still checked and billed every run.
- **Jobs:** about $0.05 per company on every run, whether or not anything changed (see the Actor's [Apify page](https://apify.com/hirepulse/ats-job-pulse)). That is about $3 a month for 2 companies, and about $37.50 a month for 25.
- **Free Apify plan:** the ads Actor gives a free preview of 1 domain and up to 5 ads. On the free plan the same few ads can come back every day as "new", and the summary says so. It is enough to see the output, not to run a daily watchlist.

Limits built into the script:

- Every Actor run gets a hard cost cap (`max_charge_usd`), enforced by Apify. The script refuses to start if the caps add up to more than `max_total_usd_per_run`.
- At most 25 domains and 25 companies per run.
- If the cost cap is too low for the list, Apify skips the rest silently. The script reads the Actor's run summary and prints a warning such as "10 of 25 were checked, raise max_charge_usd", instead of saying "no changes". Check the caps against the list: 25 companies at $0.05 need more than the default $0.50.
- A failed run is reported and **never retried**, because a retry would be billed again.
- `python watchdog.py --dry-run` checks your config and prints the plan and the largest possible price. It calls nothing.

## Set it up (about 10 minutes)

1. **Copy this folder into a new private GitHub repository.**
2. **Make your config.** Copy `watchdog.config.example.json` to `watchdog.config.json`, and list your competitors:
   ```json
   {
     "ads":  { "domains": ["competitor-one.com"], "max_ads_per_domain": 20, "max_charge_usd": 0.5 },
     "jobs": { "companies": ["competitor-one.com"], "max_charge_usd": 0.5 },
     "max_total_usd_per_run": 1.0
   }
   ```
   Commit it. It holds no secrets. The workflow does nothing until this file exists. `jobs.companies` takes a domain such as `stripe.com`, or a board slug such as `greenhouse:stripe`. `jobs.title_keywords` (for example `["sales", "data"]`) limits the jobs list to matching titles.
3. **Add the secrets** in GitHub: Settings, Secrets and variables, Actions, New repository secret.
   - `APIFY_TOKEN`: your Apify API token (Apify Console, Settings, API and integrations). Use a token you can delete later.
   - `SLACK_WEBHOOK_URL` (optional): a Slack incoming webhook. Without it, the summary only appears on the run's page in GitHub.
4. **Run it once as a baseline.** In the Actions tab, choose `competitor-watchdog`, Run workflow, and tick `baseline`. It runs, saves the starting point, and posts nothing.
5. **Done.** From the next morning (06:00 UTC, change the `cron` line in `.github/workflows/watchdog.yml`) it posts only what changed.

You can also run it on your own computer: set `APIFY_TOKEN` in the environment and run `python watchdog.py --config watchdog.config.json`. Python 3.10 or later, no packages to install.

## How it works

- The ads call uses `only_new_ads`, so Apify remembers in your account which ads it has already returned for each domain.
- The jobs call uses `track_diff` and `only_changes` with a `state_id`, so Apify remembers each company's jobs in your account and returns only the NEW and CLOSED ones. On the first run every job is new, so that run is labelled as the starting point.
- The token is sent only to `api.apify.com`, in the `Authorization` header. It is never printed or written to a file.
- The Slack webhook must start with `https://hooks.slack.com/`.

## Notes

- GitHub pauses scheduled workflows in a repository that has had no activity for 60 days. Re-enable it in the Actions tab if that happens.
- Ads are shown as Google's Transparency Center lists them. Google does not publish spend or impression numbers for commercial ads.
- A domain can return ads from advertisers other than the brand itself (resellers, agencies, affiliates).

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests run offline. They replace the Apify API, so they spend nothing.

## License

MIT.
