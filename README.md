# NBA 9-Cat Draft Ranker

WARNING: COMPLETELY VIBE CODED

Turn a generic top-200 player list into **your** draft ranking via head-to-head
comparisons. The app shows two players side-by-side with all nine category
stats — the better value highlighted — and you pick which one you'd draft
first. Repeat ~300–500 times and you have a personal, fully sorted draft list.

Nine categories, shown in classic order: FG%, FT%, 3PM, PTS, REB, AST, STL,
BLK, TO (lower is better).

Python 3.9+ standard library only — no install, no dependencies.

## Quick start

```bash
python3 fetch_data.py            # (optional) refresh players.csv — current-season projections
python3 draft_sort.py --n 12     # a 12-player trial run — try this first
python3 draft_sort.py            # rank the top 200 (resumable, ~30-45 min)
```

`fetch_data.py` downloads FantasyPros' free consensus **per-game
projections for the current season** (~266 players, no login needed) and
writes `players.csv` in projected-total-z order. The bundled `players.csv`
already comes from there. The sorter itself never touches the network — it
only reads `players.csv`.

## The comparison screen

```
  Q 137 · 68/200 placed (34%) · 12m41s   [quick check vs current worst]
  punt: none   ·   comparisons 136 · ties 9 · skips 0 · undos 1
════════════════════════════════════════════════════════════════════════════════
   [1] LEFT · challenger                           [2] RIGHT · #12 of 68 placed
   #41 Scottie Barnes   TOR F·65 GP            #57 Jalen Williams   OKC F·68 GP
────────────────────────────────────────────────────────────────────────────────
                    z       LEFT  │ CATEGORY │       RIGHT       z
                +0.02      0.472  │   FG%    │  *    0.518 * +0.61
                +0.18      0.812  │   FT%    │  *    0.889 * +0.77
              * +0.44 *      1.9  │   3PM    │         1.8   +0.10
              * +0.72 *     22.4  │   PTS    │        19.8   +0.31
              * +0.55 *      8.1  │   REB    │         5.4   -0.41
                -0.12        6.0  │   AST    │  *      6.9 * +0.88
                +0.31        1.4  │   STL    │         1.4   +0.31
                -0.22        0.8  │   BLK    │  *      1.7 * +0.95
                -0.35        2.4  │    TO    │  *      1.9 * +0.41
────────────────────────────────────────────────────────────────────────────────
              TOTAL Z  +1.53      │ Δ +2.40  │       +3.93 TOTAL Z
                                RIGHT is better
────────────────────────────────────────────────────────────────────────────────
   [1] left        [2] right       [t] tie         [u] undo
   [s] skip        [r] rankings    [?] help        [q] save & quit  [e] export

   t: tie — RIGHT (already ranked) stays above   * = better · TO: lower is better
```

- **The LEFT side is always the challenger** (the player currently being
  placed); the RIGHT side is a player already in your ranking — its header
  shows which slot is being contested (`#12 of 68 placed`).
- **GP** next to the position is games played — a low number means a small
  sample (and likely injury history); per-game rates from <15 games are
  noisy, which is what `--min-g 15` filters on.
- Green bold (or `*` without color) marks the better raw value; **TO is
  lower-is-better**. Equal values get no highlight.
- **z** puts each category on the same scale (0 = pool average, +1 = one
  standard deviation above it). **TOTAL Z** sums them — use it as a
  tiebreaker, not a boss. FG%/FT% z is volume-weighted by attempts (a
  12-for-18 night matters more than a 2-for-3 night).
- **Δ** = RIGHT's total z minus LEFT's total z, with a plain-language
  verdict.

## Keys

| Key | Action |
|-----|--------|
| `1` / `l` | left player is better |
| `2` | right player is better |
| `t` | about equal (tie) |
| `u` | undo the last player placement (they get re-asked) |
| `y` | redo the last undo — only shown while an undo can be taken back |
| `e` | export the current ranking to `export.txt` (name, position, z, stats) |
| `s` | skip the challenger to the end of the queue |
| `r` | show your ranking so far |
| `?` / `h` | help |
| `q` | save progress and exit (resume any time) |
| Ctrl+C / Ctrl+D | same as `q` (progress saved) |

## Tie semantics

A tie means the **already-ranked (RIGHT) player stays above** the challenger.
Because the ranked player was placed earlier, ties silently preserve the
seed order among players you're indifferent to — deterministic and
defensible. If you change your mind mid-session, use `u`.

## Punting

`--punt to,ft_pct` ignores those categories in the z totals (raw stats
still show, dimmed, with `—` instead of a z). Valid names: `fg%/fg,
ft%/ft, three/3pm, pts, reb, ast, stl, blk, to/tov`. Punt affects display
and totals only.

## Resume, undo, and state.json

Progress is saved **after every answer** to `state.json` (atomic writes, so
Ctrl+C mid-write can't corrupt it). Quitting mid-question is safe: the
current challenger is simply re-asked next session (≤ ~8 wasted questions,
paid once). If `players.csv` changes after you started, you'll be asked to
continue with saved progress or restart. Undo (and redo) work across
sessions too: undo pops the last placement onto a redo stack and re-asks
the player, `y` restores the placement instantly, and any new placement
clears the redo history.

## Output

When the queue is empty, `draft_list.txt` is written: numbered 1–200 with
team, position, total z, and seed rank, plus a summary of your biggest
risers and fallers vs the seed list.

**Mid-run export:** press `e` at any question to snapshot the current
ranking to `export.txt` — player names first, then position, total z, and
the nine stats:

```
      NAME                       POS        Z    FG%    FT%    3PM    PTS    REB    AST    STL    BLK     TO
   1. Nikola Jokic               C     +11.66  0.569  0.831    1.7   27.7   12.9   10.7    1.4    0.8    3.7
```

`--export FILE` changes the destination.

## Using your own CSV

`players.csv` is plain CSV with a header row (comment lines starting with
`#` are fine). Required columns:

```
rank,name,team,pos,inj,inj_note,g,mpg,pts,three,reb,ast,stl,blk,fg_pct,fga,ft_pct,fta,to,usg,bm_value,bm_id
1,Nikola Jokic,DEN,C,,,65,34.8,27.7,1.7,12.9,10.7,1.4,0.8,0.569,17.4,0.831,7.4,3.7,31.0,1.12,3930
```

- Only `name` + the 9 stats are strictly required (`fga`/`fta` are needed
  for volume-weighted percentage z; without them the app warns and falls
  back to plain percentage z). `rank` sets the starting order; missing
  ranks are assigned by row order.
- Headers are normalized loosely (`3PM`→three, `GP`→g, `FG%`→fg_pct,
  `TOV`→to, …). Percentages written as 0–100 (e.g. `56.9`) are converted
  automatically.
- `--min-g 15` drops low-games players whose per-game rates are
  small-sample noise (19 of the 234 seeded players have <15 games).
- The startup sanity table (min/mean/max per category) is there to catch
  percent-vs-fraction or per-game-vs-total mixups at a glance.

## How the ranking works

An adaptive insertion sort seeded by the projected order. For each player
in seed order, one question vs the current worst-placed player is asked;
if you agree the challenger is worse (the common case), it's placed at the
end — **1 question per player ≈ 200 total** when you mostly agree with the
seed. Upsets trigger a binary search (≤ ~8 more questions). Realistic
sessions: 250–500 questions; worst case ~1,500. At 3–5 s per answer, the
full 200 takes 20–40 minutes — split it across sittings (resume works) or
use `--n 120` to rank just your draft-relevant depth.

## What the seed is (and isn't)

The seed is a **projection list, not a truth**: it only sets the starting
order and the z reference — your answers are ground truth. Sources:

- **FantasyPros consensus** (default, via `fetch_data.py`) — free per-game
  projections for the current season, ~266 players, updated regularly. It
  publishes no FGA/FTA, so FG%/FT% z-scores are unweighted.
- **ESPN** — projections API bounces anonymous requests (needs an ESPN
  account's cookies), so there's no fetcher for it.
- **Your own CSV** — e.g., a Hashtag Basketball export if you subscribe;
  drop it in as `players.csv` with `--restart`.

## Troubleshooting

- **A fetcher fails loudly** ("parsed 0 player rows") → the site's markup
  changed. Save the page and try `--parse-file page.html`, or supply your
  own CSV.
- **Narrow terminal** (< 78 columns) → the screen switches to a stacked
  layout automatically.
- **No color** → `--no-color`, or set `NO_COLOR=1`; winners are marked
  with `*` instead.
- **Unicode names** (e.g. Jović) degrade to ASCII on terminals that can't
  print them rather than crashing.
- **State won't load** → the saved progress doesn't match the current
  `players.csv`; remove `state.json` or use `--restart` (your CSV is never
  touched).

## Selftest

```bash
python3 draft_sort.py --selftest   # algorithm + z + CSV + state tests
python3 fetch_data.py --selftest   # FantasyPros parser test
```

The sorter selftest runs the full algorithm with an auto-answerer that
answers by total z and verifies the final order equals the z-sorted order
across near-seed, reversed, and shuffled seeds — plus undo, skip, tie,
quit/resume, punt math, volume weighting, and state validation.

## FAQ

- **Why z-scores?** Raw counting stats live on different scales; z puts
  them on one. It's an aid, not an answer — you're the drafter.
- **Why volume-weight FG%/FT%?** A 2-for-3 night and a 12-for-18 night
  would otherwise score identically.
- **Why do ties favor the already-ranked player?** It preserves the seed
  order among players you're indifferent to, and keeps the tie rule
  identical on every screen.
- **Why no mid-search resume?** Quitting mid-placement discards at most a
  handful of questions and keeps `state.json` flat and inspectable.
- **Is the fetching polite?** One request per fetcher run, with a browser
  User-Agent; the sorter itself never hits the network.

*FantasyPros is a third-party site with its own terms; the fetch script is
a thin, single-request reader of its free public page.*
