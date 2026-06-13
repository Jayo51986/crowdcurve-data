# CrowdCurve data engine

Free, serverless data pipeline for the CrowdCurve WordPress plugin.
Polls Polymarket, Kalshi, and Binance/CoinGecko every 20 minutes via
GitHub Actions, and serves the results as JSON straight from this repo.
No servers, no API keys, no cost.

## Setup (about 10 minutes, one time)

1. Create a **public** GitHub repository named `crowdcurve-data`
   (public is required so the raw JSON URLs are readable).
2. Upload the contents of this folder to the repo root:
   `poller.py` and `.github/workflows/poll.yml` (keep the folder path).
3. Go to the repo's **Actions** tab. If prompted, enable workflows.
4. Open the "CrowdCurve data engine" workflow and press **Run workflow**
   (the manual trigger). Wait ~2 minutes for it to finish green.
5. Confirm a `data/` folder appeared in the repo with `config.json`,
   per-coin folders, and `history/`. Open `data/log.json` to see what
   the run found.
6. Connect WordPress: add this snippet via the Code Snippets plugin or
   your child theme's functions.php, replacing YOURUSER:

   ```php
   add_filter( 'crowdcurve_data_source', function () {
       return 'https://raw.githubusercontent.com/YOURUSER/crowdcurve-data/main/data';
   } );
   ```

7. Purge your site cache. The badge on every CrowdCurve page flips from
   "preview data" to "live data", and the numbers are now real.

From then on it runs itself every 20 minutes. Every UTC day also writes a
snapshot into `data/history/` - that is your historical record for the
time machine, the accuracy chart, and the calibration scorecard. It can
never be backfilled, which is why starting the engine early matters.

## What gets published

- `data/config.json` - spot prices, coverage tier, and per-horizon
  threshold markets for all 11 coins (consumed by the plugin's charts)
- `data/{COIN}/{HORIZON}` - SEO summary text + key numbers
  (consumed by the plugin's server-rendered copy), horizons W M Q Y Y1 Y2
- `data/history/{date}.json` - daily snapshot archive
- `data/log.json` - the last run's log, for debugging

## Notes and troubleshooting

- GitHub cron is best-effort; runs may drift a few minutes. Fine.
- raw.githubusercontent.com caches for ~5 minutes; combined with the
  plugin's 10-minute transient, expect numbers to be at most ~30 min old.
- Coverage is whatever the markets offer: BTC/ETH usually rich, smaller
  coins often spot-only. The plugin renders honestly either way.
- If a run fails or parses zero markets, open the Actions log and
  `data/log.json` - the poller logs every fetch and every skip reason.
  API response shapes occasionally change; the parsers are written
  defensively, but the first live run is the real test.
- Want faster updates later? Lower the cron to */15. Want history more
  than daily? Change the history filename to include the hour.
