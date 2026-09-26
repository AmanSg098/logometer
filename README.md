# logometer

**Tail your logs. Catch anomalies. Skip the 2am grep.**

`logometer` watches a log file (or stdin), buckets lines into time windows, and flags windows that look statistically off — an error spike, or an error type that's never shown up before. It's silent when things are fine. No AI required for the core detection; point it at an LLM with `--explain` if you want a plain-English guess at *why* a window looks weird.

Zero dependencies for the core tool — just Python 3.10+.

<!-- TODO: record a short asciinema/GIF demo and drop it here before publishing.
     Suggested recording: `logometer tail examples/sample.log --replay` -->

## Install

```bash
git clone https://github.com/<your-username>/logometer.git
cd logometer
pip install -e .
```

## Quick start

Try it on the included sample log (has a normal-traffic baseline plus an injected error burst):

```bash
python3 -m logometer.cli tail examples/sample.log --replay
```

or, once installed:

```bash
logometer tail examples/sample.log --replay
```

## Usage

```bash
# Tail a live log file (like tail -f, but with anomaly detection)
logometer tail /var/log/app.log

# Pipe from stdin
tail -f app.log | logometer tail -

# Replay a static file from start to finish, then exit
logometer tail app.log --replay

# Only show anomalous windows, hide the "ok" noise
logometer tail app.log --replay --quiet

# Adjust window size (seconds) and how aggressively to flag deviations
logometer tail app.log --window 30 --sensitivity high

# Mute known-noisy lines so they never reach the baseline (repeatable regex)
logometer tail app.log --ignore 'healthcheck' --ignore 'DeprecationWarning'

# Ask an LLM for a one-sentence explanation of each anomaly
export ANTHROPIC_API_KEY=your-key-here
logometer tail app.log --explain

# JSON output, one object per line — good for piping into other tools
logometer tail app.log --format json
```

### `--explain`

Requires `ANTHROPIC_API_KEY` (default) or `OPENAI_API_KEY` (with `--explain-provider openai`) set in your environment. No SDK install needed — it's a plain HTTPS call. If the key is missing or the request fails, `logometer` prints a one-line warning to stderr and keeps tailing normally; it never crashes because `--explain` had a bad day.

### `--ignore`

Real logs usually have one or two messages that fire constantly and mean nothing. Left alone they dominate the error count, so the rolling baseline learns them as "normal" and a genuine problem has to shout louder to get noticed. `--ignore` drops matching lines before any analysis:

```bash
# on a real macOS install log, one benign repeating message accounted for
# 97% of all "errors" — muting it cut the noise dramatically
logometer tail /var/log/install.log --replay --window 60 --quiet \
  --ignore 'installation-check'          # 27 anomalies -> 5, all genuine
```

Each `--ignore` takes a regular expression and can be repeated. An invalid pattern is reported as a normal CLI error rather than a crash.

### Live tailing

`logometer tail app.log` (without `--replay`) follows the file the way `tail -f` does, with two things a naive tail loop gets wrong:

- **Windows close on time.** A window is emitted once its duration has elapsed, even if no further lines arrive. Without this, a service that errors and then dies would never report that final burst — the most important one — because nothing follows it to trigger the flush.
- **Rotation is handled.** If the log is rotated out from under it (`logrotate`, or truncated in place with `copytruncate`), it notices and follows the new file instead of reading a now-orphaned file forever.

Output is flushed as each window is reported, so piping or redirecting works in real time:

```bash
logometer tail app.log --quiet --format json >> alerts.jsonl
tail -f app.log | logometer tail -
```

### Pretty output

If you `pip install rich` (or `pip install -e ".[pretty]"`), output automatically upgrades to styled panels. Not required — plain ANSI output works everywhere.

## Example output

```
$ logometer tail app.log --replay

  [12:03:10 – 12:03:20]  ok        errors: 1   baseline: ~1.2

  [12:03:20 – 12:03:30]  ! ANOMALY  errors: 14  baseline: ~1.2   score: 4.8x
    New error signature detected: 'ConnectionResetError: [Errno <x>] Connection reset by peer id=<x>'

  [12:03:30 – 12:03:40]  ok        errors: 2   baseline: ~1.4
```

With `--explain`:

```
  [12:03:20 – 12:03:30]  ! ANOMALY  errors: 14  baseline: ~1.2   score: 4.8x
    New error signature detected: 'ConnectionResetError: [Errno <x>] Connection reset by peer id=<x>'
    Explanation: Downstream connections are being reset mid-request, likely an upstream outage.
```

## How the detection works

1. Lines are batched into fixed-size windows — by wall-clock time if timestamps are parseable, otherwise by a fixed line count.
2. Each window is fingerprinted: error count, warning count, and the set of distinct error "shapes" (ids/numbers/timestamps stripped, so the same underlying error collapses to one shape regardless of the specific id).
3. A rolling baseline (mean + standard deviation over recent windows) tracks what "normal" error volume looks like.
4. A window is flagged if its error count deviates meaningfully from that baseline, or if it contains an error shape never seen before in this run.

No ML model, no training step — deliberately simple and explainable. See `ARCHITECTURE.md` for the full design rationale and the reasoning behind specific tuning choices (like the noise floor on the standard deviation).

## Known limitations

Worth knowing before you point this at something you care about. These are real, tested behaviours, not hypotheticals:

- **Severity is keyword matching, not parsing.** A line counts as an error if it contains a word like `error`, `fatal` or `critical`. That means `Downloading 1 products: Critical []` — an *empty* list, i.e. good news — is counted as an error. Use `--ignore` to mute these.
- **Python tracebacks are only partly seen.** `Traceback (most recent call last):` is detected, but the final `ValueError: ...` line is not, because the match looks for `error` as a standalone word. And since that first line is identical for every exception, new-signature detection can't tell one crash type from another. Logs that print their own level (`ERROR ValueError: ...`) work properly.
- **JSON logs break new-signature detection.** Fingerprinting strips quoted strings, which in a JSON line removes the entire message — so unrelated errors collapse into one shape. Error *counting* still works. Extracting the message first works around it: `jq -r '.level + " " + .msg' app.log | logometer tail -`.
- **Only the error count is scored.** A flood of identical *warnings* won't trigger a spike, and a sudden *drop* in traffic isn't detected either — only rises above the baseline are.
- **Window labels show time, not date.** On a log spanning several days you'll see the same `[17:14:39 – 17:15:39]` label more than once.
- **Timezone offsets are ignored.** `2026-09-20 19:05:04+05:30` is read as local wall-clock time; a log mixing offsets is bucketed as though they were the same clock.
- **Nothing persists between runs.** The baseline and the set of known error shapes are rebuilt from scratch each start, so expect the first few windows of any run to over-flag. (Persistence is on the roadmap.)
- **If timestamps can't be parsed** it silently falls back to fixed 50-line windows, and `--window` stops having any effect. You can tell from the labels: `[line 1 – line 50]` instead of a time range.
- **`--explain` sends log lines to a third party.** There's no redaction — don't use it on logs containing secrets or personal data.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

## Roadmap

- Config file for custom log-format parsing
- Multiple file / glob support
- Slack/Discord webhook alerts
- Persistent anomaly history (SQLite)

## Contributing

Issues and PRs welcome — especially around log-format parsing (every stack logs differently) and reducing false positives.

## License

MIT
