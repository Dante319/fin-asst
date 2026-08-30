# fin-asst

A personal finance assistant that runs entirely on your own machine.

Import CSV statements, see where the money actually goes, and project several
savings goals that compete for the same monthly surplus. No bank connections,
no API keys, no subscription, no data leaving the laptop. Python and SQLite,
nothing else.

## Why it works this way

**Your statements never leave your machine.** Transactions are read from CSVs
you export yourself and stored in a SQLite file in `data/`, which is gitignored.
The web app binds to `127.0.0.1`. There is no telemetry and no third party.

**Categorisation is deterministic.** A transaction gets a category because a
rule you can read matched it — not because a model guessed. When you correct
one, the correction is saved as a top-priority rule and back-applied to that
merchant, so the same mistake never happens twice. This keeps every number
reproducible and auditable, which is the whole point of a finance tool.

**Goals compete.** Projecting each goal on its own always flatters you. Here
they all draw on one monthly surplus in priority order, month by month, and the
output is how many months each goal *slips* — not a comforting per-goal figure.

## Quick start

Dependencies are managed with [uv](https://docs.astral.sh/uv/). It handles the
Python version, the virtualenv and the lockfile in one tool, and it sidesteps
the "externally-managed-environment" wall that macOS system Python puts in front
of `pip install`.

```bash
brew install uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh

cd fin-asst
uv sync                  # creates .venv and installs from uv.lock

# Try it on synthetic data first (fake, committed to the repo on purpose)
uv run python scripts/gen_synthetic.py
uv run finasst init
uv run finasst import scripts/samples/sample_amex_simplycash.csv --account "Amex SimplyCash"
uv run finasst import scripts/samples/sample_simplii_chequing.csv --account "Simplii Chequing" --kind chequing
uv run finasst summary

# Then the web app
uv run finasst serve     # http://127.0.0.1:8000
```

`uv run` puts the command in the project environment for you, so there is no
venv to activate and nothing to install globally. `uv sync` installs exactly
what `uv.lock` pins, so the environment is reproducible; `uv add <package>`
updates both the manifest and the lock.

If you prefer an activated shell: `source .venv/bin/activate` after `uv sync`,
then run `finasst ...` directly. Once the environment exists, `.venv/bin/finasst`
is faster than `uv run finasst`, which re-verifies the project on every call.

### If the project folder is in iCloud Drive (or Dropbox, or OneDrive)

macOS syncs `~/Documents` to iCloud Drive by default, and that combination is
worth avoiding for two separate reasons.

**The virtualenv.** `.venv` is thousands of small files. Inside a synced folder,
every Python import competes with the sync daemon, and a first run can stall for
minutes with no output at all. Keep the code where it is and move the
environment out:

```bash
export UV_PROJECT_ENVIRONMENT=~/.venvs/fin-asst   # add to ~/.zshrc to make it stick
uv sync
~/.venvs/fin-asst/bin/finasst serve
```

**The database.** This one matters more. A SQLite file in a continuously-synced
folder is a genuine corruption risk: the sync daemon can upload a half-written
database mid-transaction, and two machines can resurrect each other's stale
copies. So the database defaults to your user data directory, outside the repo:

| Platform | Default location |
|---|---|
| macOS | `~/Library/Application Support/fin-asst/finasst.db` |
| Linux | `~/.local/share/fin-asst/finasst.db` |
| Windows | `%LOCALAPPDATA%\fin-asst\finasst.db` |

An existing `data/finasst.db` from an earlier version still takes precedence, so
upgrading never orphans a database you have been using. To move an old one onto
the new default:

```bash
mkdir -p ~/Library/Application\ Support/fin-asst
mv data/finasst.db ~/Library/Application\ Support/fin-asst/finasst.db
```

`FINASST_DB` overrides the file outright and `FINASST_DATA_DIR` overrides the
directory, if you would rather put it somewhere specific. Back it up
deliberately -- copy the file, or re-import your statements, which is
idempotent.

## Using your own statements

Export a CSV from each account and import it. Two formats are recognised
automatically, plus a fallback:

| Source | Notes |
|---|---|
| **Amex Canada** | One export format across Cobalt and SimplyCash. Amex writes a purchase as a positive number; the importer flips it so an outflow is always negative. |
| **Simplii** | Headerless CIBC-style layout: date, details, funds out, funds in, balance. |
| **Anything else** | Column-guessing fallback. Needs a header row with a recognisable date column and either a signed amount or debit/credit columns. |

Re-importing a file you have already loaded does nothing, and two exports that
overlap by a few days do not double-count — each transaction has a fingerprint
built from account, date, description and amount.

The one deliberate trade-off: two genuinely identical charges on the same day at
the same merchant for the same amount collapse into one. Silently doubling your
spending when you re-import a statement would be the worse failure.

## Commands

```
finasst init                       create the database, seed the rules
finasst import FILE... --account   load statements
finasst categorize [--recategorize]
finasst review                     what no rule matched
finasst set ID "Category"          fix one, and teach the merchant
finasst summary [--month] [--months N]
finasst goal add NAME --amount N --date YYYY-MM-DD [--priority N] [--monthly-min N]
finasst goals
finasst project [--surplus N]
finasst whatif --without "Goal name"
finasst serve
```

## How the projection works

Each simulated month, the surplus is handed out in this order:

1. every goal gets its `monthly_min` floor, in priority order — the way to
   protect a low-priority goal from being starved entirely;
2. the rest is offered to goals in priority order, each taking what it needs to
   stay on schedule for its target date (an undated goal asks for its remaining
   gap spread over 24 months, so priority still means something for it);
3. anything left accelerates the highest-priority unfinished goal.

Balances compound at the return rate you set; targets quoted in today's dollars
are inflated to the year they are actually reached, so a 2031 house shows what
it will cost in 2031. Both rates are levers on the Goals page — set the return
to 0 once to see the floor.

The surplus itself comes from your transactions: income minus spending over the
last few complete months, ignoring the current partial month and excluding
transfers between your own accounts. If a lot of spending is still
uncategorised, the app says so rather than quietly reporting a number built on
it.

## Layout

```
finasst/
  db.py            SQLite schema and helpers
  config.py        paths, categories, the sign convention
  importers/       amex.py, simplii.py, generic.py + registry
  categorize.py    seeded rules, matching, learned user rules
  analytics.py     measured facts: totals, breakdowns, recurring charges
  goals.py         the projection model
  cli.py           command line
  web/             FastAPI app, Jinja templates, one CSS file, ~25 lines of JS
scripts/           synthetic statement generator + samples
tests/             24 tests: sign conventions, dedup, rule precedence, goal maths
pyproject.toml     dependencies and the finasst entry point
uv.lock            exact pinned versions -- committed, so the environment is reproducible
```

The front end has no build step, no npm and no CDN — the page loads with the
machine offline.

## Tests

```bash
uv run pytest
```

## Not doing (and why)

- **Bank auto-sync.** Every option costs money at any real volume or hands a
  third party your banking credentials.
- **LLM categorisation inside the app.** It would make the numbers
  irreproducible and put transaction data on someone else's server. Point Claude
  at the database when you want analysis; the app itself stays deterministic.
- **Multi-user or hosted deployment.** There is no auth because there is nothing
  to authenticate. Do not expose this to a network.
