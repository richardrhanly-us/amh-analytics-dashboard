# Setting a sorter's bin layout (UI2 v6)

How an operator gives one sorter a configured bin layout, so Live Today's "Sorter at a glance" draws every bin of that
machine in its physical place, including bins with no check-ins that day. Written for the first use, NBPL's Main AMH
sorter, and reusable for any other sorter.

**Do not run any step against production until the UI2 v6 change has been reviewed, merged and deployed, and the
product owner has given an explicit go-ahead.** Until the API is deployed, a stored layout has no effect.

## What changes, and what does not

- One key, `bin_layout`, in one row of `branch_settings` (the sorter's host branch). It is changed in place
  (`jsonb_set`, or the `-` operator to remove it). No other key of that document is touched. The efficiency settings,
  internal routing and everything else stay byte-for-byte the same. The script checks this and rolls back if not.
- No schema migration, no deploy, no other table, no other sorter.
- What a layout is, and what is refused, is `src/services/bin_layout.py`. The API ignores an invalid stored layout,
  logging `bin layout ignored: org=… sorter=… reason=…`, and the sorter falls back to the observed-bins drawing.
- Rollback is the same script: `set` the previous value it printed, or `clear`.

## Where to run it

From an operator workstation with the repository checked out at the merged commit and its virtual environment.
**Never from a city-owned machine**: city policy forbids a city machine connecting to the database directly.

Keep the production URL in the environment for the session only. Do not paste it into files or shell history.

```powershell
# PowerShell, repository root
$env:DATABASE_URL = '<production Neon connection string>'
```

## NBPL's layout

From the sorter's own screen, left to right:

```text
         [Bin 6] [Bin 4] [Bin 2]
[Bin 0] ===== CONVEYOR ========
         [Bin 5] [Bin 3] [Bin 1]
```

As a layout, the end bin and then each column from the left end, `[above the belt, below it]`:

```json
{"end": "0", "columns": [["6", "5"], ["4", "3"], ["2", "1"]]}
```

Save exactly that line as a file outside the repository, for example `$HOME
bpl-main-bin-layout.json`, and pass it with
`--layout-file`. Windows PowerShell 5.1 strips the double quotes inside a JSON argument passed to a native program, so
`--layout '…'` does not work there. If it happens, the script refuses with `(not JSON)` and writes nothing. The file
is also the reviewed record of exactly what was applied.

## Step 1: identify the sorter (read-only)

Do not take the slugs from tests or documentation. Read them from production:

```powershell
.venv\Scripts\python.exe scripts\set_sorter_bin_layout.py review
```

This only reads, inside a `READ ONLY` transaction. It lists every organization with its slug, name and status. Under
each one it lists every sorter site with its slug, its branch name, its installations, and its current `bin layout:`
(`none`, the stored JSON, or `INVALID (…)`). Narrow it with `--org-slug <slug>` once you know the organization.

Record, exactly as printed:

| Item | Value from `review` |
|---|---|
| Organization slug | |
| Organization name (must be New Braunfels Public Library) | |
| Sorter site slug | |
| Branch name | |
| Installation name(s) (must be the Main AMH sorter) | |
| Current bin layout (expected `none`) | |

Stop and ask if more than one site could be the Main AMH sorter, or if the current layout is not `none`.

## Step 2: dry run (no database)

```powershell
.venv\Scripts\python.exe scripts\set_sorter_bin_layout.py set `
  --org-slug <org slug> --sorter-slug <sorter slug> --confirm-branch-name '<branch name>' `
  --layout-file $HOME
bpl-main-bin-layout.json
```

Expect `DRY RUN: would store this bin layout for …` and the layout exactly as above. A `REFUSED: --layout is not a
valid bin layout (…)` means the JSON is wrong. Nothing is written either way.

## Step 3 (recommended): rehearse on a Neon branch of production

Create a Neon branch of the production database in the Neon console. Set `DATABASE_URL` to that branch's connection
string, then run Steps 1, 2 and 4 against it. Confirm with `review` that the layout is stored and every other sorter
still shows `none`. Delete the Neon branch afterwards, then set `DATABASE_URL` back to production.

## Step 4: apply (one transaction)

Only with the go-ahead:

```powershell
.venv\Scripts\python.exe scripts\set_sorter_bin_layout.py set `
  --org-slug <org slug> --sorter-slug <sorter slug> --confirm-branch-name '<branch name>' `
  --layout-file $HOME
bpl-main-bin-layout.json --execute
```

Expect:

```text
Layout stored before: null
STORED the bin layout of org_slug='…' sorter_slug='…': {"end": "0", "columns": [["6", "5"], ["4", "3"], ["2", "1"]]}
```

**Keep the `Layout stored before:` line**: it is the rollback value. The change is refused, and nothing is written, if:

- the slugs do not name exactly one active sorter site;
- `--confirm-branch-name` is not that site's branch name exactly;
- the document after the change differs from before in anything but `bin_layout`.

Re-running the same command is safe: it stores the same value again.

## Step 5: verify

1. `review --org-slug <org slug>` shows the layout under the right site, and any other site there still shows `none`.
2. In the customer app, open the Main AMH sorter's Live Today (a full page reload; the organization is read once per
   load). Check that "Sorter at a glance":
   - shows seven bins: Bin 0 upright at the left end, 6/4/2 above the belt and 5/3/1 below, at desktop width;
   - shows bins with no check-ins today as `0`, lightest shading, empty fill;
   - leaves the counts in "Bin volume today" and today's figures as they were;
   - draws any bin outside 0 to 6 that received check-ins after the seven, not hidden.
3. Open any other sorter (another organization if you can see one). It should show only its observed bins, as before.
4. Check the API logs for `bin layout ignored`: there should be none for this sorter.

## Rollback

Remove the layout (the sorter returns to observed bins only):

```powershell
.venv\Scripts\python.exe scripts\set_sorter_bin_layout.py clear `
  --org-slug <org slug> --sorter-slug <sorter slug> --confirm-branch-name '<branch name>' --execute
```

To restore an earlier layout instead, save the JSON from that earlier `Layout stored before:` line as a file and
run `set --layout-file` with it.

Afterwards, clear the variable: `Remove-Item Env:DATABASE_URL`.
