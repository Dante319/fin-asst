# fin-asst

A personal finance assistant that runs entirely on your own machine.

[MIT licensed](LICENSE) -- free to clone, run, and modify. Each person who runs it keeps their own database on their own machine; nothing is shared between installs.

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

If you have `make`, the wrapper handles the environment for you and keeps it out
of iCloud, which is the cause of nearly every environment problem on this
project:

```bash
make sync      # install into ~/.venvs/fin-asst
make serve     # http://127.0.0.1:8000
make test
make doctor    # when something is broken, start here
```

The longer form, and what `make` is doing:

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

Import a CSV, an Excel workbook (.xlsx/.xls), or a PDF statement -- the file
extension picks the route, and `--issuer` forces a specific parser if
auto-detection guesses wrong. CSV and Excel go through the same
column-detecting importers (Excel is just read into the same shape a CSV
export would be); PDF statements each need their own hand-written parser,
since there is no header row to detect from -- just extracted text.

| Source | Notes |
|---|---|
| **Amex Canada** | CSV. One export format across Cobalt and SimplyCash. Amex writes a purchase as a positive number; the importer flips it so an outflow is always negative. |
| **Simplii (CSV)** | Headerless CIBC-style layout: date, details, funds out, funds in, balance. |
| **Simplii credit card (PDF)** | The Cash Back Visa statement. Charges are two physical lines (date/merchant, then category/amount); payments are one line. Reconciles to the statement's own previous/new balance. |
| **Simplii chequing (PDF)** | The no-fee chequing account statement. Text extraction jams the two amount columns together with no separator, so direction and amount are derived from the running balance column instead -- see the comment in `simplii_pdf.py` if a future layout change breaks this. Reconciles to the statement's total funds in/out and closing balance. |
| **Amex year-end summary** | CSV. A whole year in one file, with Amex's own categories. Dates are DD/MM/YYYY and are parsed as such rather than guessed. |
| **EQ Bank** | PDF. EQ has no CSV export. Text is extracted and every statement must reconcile: opening balance + every parsed transaction has to equal the stated closing balance, or the import is refused. |
| **Anything else (CSV/Excel)** | Column-guessing fallback. Needs a header row with a recognisable date column and either a signed amount or debit/credit columns. |

Every PDF importer follows the same rule: a parse that cannot be checked
against the statement's own totals is not trusted, and a statement that does
not reconcile is refused rather than imported with a silent gap.

Re-importing a file you have already loaded does nothing, and two exports that
overlap by a few days do not double-count — each transaction has a fingerprint
built from account, date, description and amount.

The one deliberate trade-off: two genuinely identical charges on the same day at
the same merchant for the same amount collapse into one. Silently doubling your
spending when you re-import a statement would be the worse failure.

## Knowing what the numbers do not cover

Two features exist because a finance tool that quietly reports on partial data is
worse than one that reports nothing.

**Coverage.** `finasst coverage` matches outgoing transfers against the incoming
side in another account and reports everything left over -- money that went
somewhere no statement covers. It is shown on the dashboard next to your
spending, so the size of the blind spot is never hidden. Importing the account on
the receiving end turns a gap into a matched internal movement.

**Income regimes.** The surplus is an average over recent months. If a income
stream ended partway through those months, that average describes a situation
that no longer exists, so the app says so rather than projecting from it.

## Remittances

For money sent abroad, a statement records only the CAD that left. What arrived
has to come from you, and without it the true cost is unknowable -- a transfer
advertising "no fee" still takes its margin in the exchange rate.

```bash
finasst fx list                          # every transfer; blanks are what is missing
finasst fx set 412 --received 61200      # what actually landed, in INR
finasst fx rates                         # optional reference rates (ECB, free, no account)
finasst fx report                        # blended rate and what the spread cost
```

`finasst fx rates` is the only command in this app that uses the network, and it
only ever fetches published exchange rates -- never anything about you.

## Commands

```
finasst init                       create the database, seed the rules
finasst import FILE... --account   load statements
finasst categorize [--recategorize]
finasst review                     what no rule matched
finasst set ID "Category"          fix one, and teach the merchant
finasst summary [--month] [--months N]
finasst insights [--limit N]       findings the app can defend, most serious first
finasst forecast [--months N]      the next few months, with the range that matters
finasst rules [--explain "TEXT"]   the rules that do the filing, and which one wins
finasst suggest [--list] [--accept KEY]   optional model: propose rules for the backlog
finasst coverage [--since YYYY-MM-DD]
finasst fx list | set ID --received N | rates | report
finasst goal add NAME --amount N --date YYYY-MM-DD [--priority N] [--monthly-min N]
finasst goals
finasst project [--surplus N]
finasst whatif --without "Goal name"
finasst mcp                        answer Claude's questions over MCP (read-only)
finasst serve
```

## Asking Claude about it

`finasst mcp` exposes this app's own calculations to Claude as a set of tools, so
you can ask questions the pages do not answer -- "what changed between June and
July", "which subscriptions did I start this year", "what would get my committed
costs below a third of my income".

Add this to `claude_desktop_config.json` (Claude Desktop -> Settings ->
Developer -> Edit Config), with the absolute path to the `finasst` in your
virtualenv:

```json
{
  "mcpServers": {
    "fin-asst": {
      "command": "/Users/you/.venvs/fin-asst/bin/finasst",
      "args": ["mcp"]
    }
  }
}
```

The design constraint is the one the rest of the app already follows: **the
model never does arithmetic.** It picks a question from a fixed catalogue and
gets back the figure this app would put on the page, computed by the same code.
It cannot sum a list of transactions and call that your grocery bill, because
`search_transactions` hands back a capped page and says so in the reply.

Three properties worth knowing:

- **Read-only, enforced by SQLite.** The connection is opened in SQLite's own
  read-only mode, so it is a guarantee rather than a promise. Claude can explain
  and suggest; categorising, adding rules and editing goals stay yours.
- **Caveats travel with every number.** Each result carries the window it covers
  and flags uncategorised spending, money leaving for accounts you have not
  imported, and an income change mid-window. A figure that arrives without its
  qualifications gets quoted without them.
- **The database never leaves your machine.** The server runs locally over this
  process's own pipes. What does reach Anthropic is the answers to the questions
  asked -- category totals, merchant names, the rows a question touches -- the
  same as anything else you put in a Claude conversation. If that is not a trade
  you want, leave the config block out; nothing else in the app depends on it.

## Findings, and the forecast

`finasst insights` (or the Insights page) runs a set of detectors over your own
history. Each finding carries the arithmetic it came from and a link to the
transactions behind it, because a finance app telling you something surprising
has to be able to prove it -- otherwise the sensible response to a surprising
claim is to distrust the app, and then the good findings go unread too.

What it looks for: the same amount charged twice days apart; a bill that has
changed price; something that has started or stopped billing you monthly; a
charge far outside what that merchant normally costs; a category well above its
own median; and how much of your income is committed before you decide anything.

Every detector stays silent below three months of history, and a category spike
caused entirely by one strange charge is reported once, not twice.

The forecast is built from three separately-sourced numbers rather than one
trend line, because they behave differently: **fixed** (merchants billing a
steady amount every month), **variable** (everything else, as a median with your
cheapest and dearest month carried through as a range), and **income** (your
declared pay if you have entered one, otherwise the average of complete months).
The range is the point. A forecast quoted as a single number invites you to
treat it as a promise.

## Rules

Every categorised transaction got its category because one rule matched it, and
`finasst rules --explain "LOBLAWS #1032 TORONTO ON"` will tell you which, and
what it beat. The Rules page lists every rule with how many transactions it
actually *decides* -- not how many it matches, since a rule that always loses to
a higher-ranked one is doing nothing for you.

Rules you teach outrank the built-in ones. Built-in rules live in the code
rather than the database, so they cannot be edited from the app: the next start
would silently undo it. Add your own for the same merchant instead.

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
  importers/       amex.py, simplii.py, simplii_pdf.py, eqbank.py, generic.py + registry
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

## Sharing this with someone else

The whole app is built around one person, one machine, one database -- so
handing it to a friend is just them doing the Quick start above with their own
copy:

```bash
git clone https://github.com/Dante319/fin-asst.git
cd fin-asst
make sync
make serve
```

There is nothing to configure to make this "theirs" -- no account, no shared
backend, no API key required. `finasst init` creates an empty database on
their machine the first time they run it, in the same per-user data directory
described above, and their statements and categorisation rules live there and
nowhere else. Two people running this independently never see a byte of each
other's data; there is no server they both talk to.

If you want to keep contributing changes back and forth, the normal GitHub
flow works: they fork the repo, or you add them as a collaborator, and pull
requests merge like any other project. The `data/` folder and every database
file are gitignored, so a `git pull` never pulls someone else's transactions.

## Not doing (and why)

- **Bank auto-sync.** Every option costs money at any real volume or hands a
  third party your banking credentials.
- **A model anywhere near a number.** There is an optional language model, and
  it has exactly three jobs, all at the edges -- the third being the MCP server
  above, which lets Claude choose which of this app's calculations to run
  without ever performing one itself. It can **propose rules** for
  merchants nothing matched -- it never writes a category onto a transaction;
  you accept a proposal, and what you get is an ordinary user rule, visible on
  the Rules page, applied by the same deterministic matcher as everything else.
  Run the same import twice and you get the same answer whether or not a model
  is configured. And it can **narrate findings that were already computed**,
  handed finished sentences with every number pre-formatted as text, so it has
  nothing to calculate and nothing to get wrong. It is off by default, and
  nothing else in the app needs it.

  Kept cheap by construction: merchants are deduplicated (200 uncategorised
  transactions are usually 20 merchants), cached forever so a merchant is sent
  at most once in the lifetime of the database, batched into one request,
  capped per run, and only ever triggered by you pressing the button. The
  reference backend is Ollama on localhost, which is free and sends nothing off
  the machine; any OpenAI-compatible endpoint works instead via
  `FINASST_LLM_URL`. An API key is read from `FINASST_LLM_KEY` in the
  environment, never written to the database.
- **Multi-user or hosted deployment.** There is no auth because there is nothing
  to authenticate. Do not expose this to a network -- this means one shared
  server for multiple people, not one person's own local instance. Each
  person running their own copy on their own machine (see "Sharing this with
  someone else" above) is exactly the intended use.
