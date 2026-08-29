# How scoring works

Every set you log gets compared against your own goal chart — a set of
targets you build for yourself when you join a challenge. Clear a target and
you earn a point. This page explains how your chart is shaped, what a point
is worth, and how the app shows you the fastest way to your next one.

This page describes **Classic** scoring — a per-lift, per-rep target table.
If your challenge runs in **Rep Target** mode instead, skip ahead to
[Rep Target scoring](#rep-target-scoring); it uses a different, simpler
formula.

## Your goal chart

When you join a challenge, you build a goal chart covering every lift the
challenge is configured on — see [Running a challenge](challenges.md) for
the four ways to set it up (published strength standards, suggested from
your lift history, manual entry, or pasting a JSON payload). However you
build it, the result
is the same shape: a target weight for each lift at each rep count from 1
to 10. Once you confirm it, your chart is locked for the rest of the
challenge — there's no editing after you join, so if a target turns out
wrong the only fix is starting a new challenge. Everything below is
measured against your own chart.

## Heavier for fewer reps, lighter for more

Your chart sets a target weight for a single rep on each lift. But you
don't have to hit that exact weight for exactly one rep to score — the app
also recognizes lighter weights done for more reps as reaching the same
target. The more reps you do, the lighter the equivalent weight target gets.

The one exception is a single rep, which always uses the full target weight
— there's no discount for going for just one.

So if you're not quite at the one-rep target, doing a few more reps at a bit
less weight can still count as hitting a point. It's the same idea behind
"a heavy single and a slightly lighter set of five can represent similar
strength" — the app does that math for you automatically.

## How a point gets awarded

For every set you log, the app checks: at the number of reps you did (capped
at 10 for scoring purposes), did your weight clear the target for that rep
count? If so, you've hit a point.

A heavy set of 12 or more reps isn't penalized for going over — if the
weight clears the toughest target, it still counts as a top score.

The app always looks for the **best possible outcome for that set** — it
checks every rep count from 1 up to 10 and gives you credit for the
highest-value point your set actually earns.

## What a point is worth

Points are richer at low reps and lighter at high reps:

- A near-max single is worth the most — 10 points.
- A set of 10 reps is worth the least that still counts — 1 point.
- Everything in between scales down step by step (a set of 5, for example,
  sits right in the middle).
- Sets outside the 1–10 rep range don't earn points.

Each set earns you the value of the single best point it reaches — points
don't stack within one set. And for your challenge score, only your
**current best result per lift** counts toward the leaderboard: if you beat
your own previous best on a lift, the new result replaces the old one. If
you log a set that doesn't beat your best, it's still saved to your history
so you can see it, but it won't move your points.

Your total challenge score is the sum of your best points across every
lift you've scored.

## Closing the gap to your next point

Earning a point takes both enough weight **and** enough reps — it's a
two-part target, not a single number. So instead of showing you one vague
distance to your next point, your performance card shows **two separate
ways to get there** — pick whichever is easier for you:

- **Add weight** — keep doing the same number of reps, but add a bit more
  weight. This is usually the smallest ask, since the target gets easier the
  more reps you're already doing.
- **Add reps** — keep the same weight, but do a few more reps. This option
  only shows up when your current weight is already enough to clear the
  easiest (10-rep) target — in that case, doing more reps at that same
  weight can be enough to reach a new point.

The app looks across your recent sets and highlights whichever one puts you
closest to your next point — not necessarily your heaviest lift, but the one
that gets you there fastest.

## "Close to goal" highlights

When one of your unscored lifts is genuinely within striking distance of its
first point, its performance card gets a **"Close to goal"** highlight — a
passive "go claim this" nudge. A lift qualifies when either:

- its remaining weight is within a small fraction of the target (by default
  **within 5%** of the rep-adjusted threshold), or
- you're only a couple of reps short at your current weight (by default
  **2 or fewer** additional reps).

The highlight is deliberately simple. It's a single on/off flag — there's no
shading or intensity scale for "how close" you are, and it never ranks or
recommends which flagged lift to go after. If several lifts qualify at once
(common late in a challenge), only the **closest 3** are highlighted so the
signal doesn't get diluted; which of them you chase on any given day is entirely
up to you.

Both thresholds are configurable per deployment via environment variables —
`CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION` (default `0.05`) and
`CHALLENGES_CLOSE_TO_GOAL_REPS_GAP` (default `2`).

## "Final stretch" endgame suggestion

In the closing days of a challenge, a standalone **"Final stretch"** card
appears between the leaderboard and the points-over-time chart — a single
motivational line restating how close you are to a point. Unlike the always-on
"Close to goal" highlight (which lives inside a per-lift performance card), this
one only appears near the end:

- It shows only in the last **14 days** before the challenge's end date (by
  default; configurable). Challenges outside that window, and ones already
  completed or cancelled, show nothing.
- It restates a gap you've already earned the right to see — the next point up
  for a lift you've already scored ("_X kg away from your next point on
  Squat_"), or the first point for one you haven't ("_X kg away from your first
  point on Squat_", or "_2 more reps at this weight would earn your first point
  on Squat_"). When an unscored lift is close on **both** counts at once — the
  weight is within the band *and* a rep or two would also cross a threshold —
  both distances are restated together ("_2.3 kg away from your first point on
  Pendlay Row, or 1 more rep at this weight_") rather than dropping one. Because
  it stands apart from the per-lift cards, it names the lift it refers to. It
  never recommends a lift to train or an exercise to do; it only restates the
  computed kg/reps gap.
- For an already-scored lift the next point always needs **more weight** (the
  next rung on your chart sits at a heavier weight and fewer reps), so the
  scored suggestion only ever talks about weight — there is no "more reps"
  variant.
- If several lifts qualify at once, only the **single closest** one (smallest
  gap relative to its target) is surfaced, so the nudge stays a single clear
  message rather than a checklist.

The window length and the two achievability thresholds are configurable per
deployment — `CHALLENGES_ENDGAME_WINDOW_DAYS` (default `14`),
`CHALLENGES_ENDGAME_GAP_FRACTION` (default `0.05`), and
`CHALLENGES_ENDGAME_REPS_GAP` (default `2`) — kept separate from the
close-to-goal settings so the two features can be tuned independently.

## Rep Target scoring

In a Rep Target challenge, your goal chart is a single **(target weight,
target reps)** pair per lift — no rep-max ladder, no weight-for-reps
tradeoff.

Scoring works differently from Classic in one key way: **weight is a gate,
not a tradeoff axis**. A set only counts at all if its weight meets or beats
your target weight — there's no substituting extra reps for missing weight
the way Classic's rep-max ladder allows. Once the weight gate is cleared,
points scale with how many of your target reps you did:

```
points = floor(10 × min(your reps, target reps) / target reps)
```

capped at 10, floored at 0. Every tier is an honest fraction of your target:
N points takes N/10 of the reps, and the full 10 takes all of them. A few
examples against a 20-rep target:

- 20 reps (or more) at or above the target weight → **10 points** (the max).
- 12 reps → **6 points**.
- 19 reps → **9 points**. The last rep is the one that earns the tenth point;
  nothing short of your whole target maxes the lift out.
- A single rep can come out at **0 points** if the target is high enough
  (e.g. 1 rep against a 999-rep target) — that's expected: the weight gate
  alone already tells you you're "on the board," and any later, better set
  still overwrites this one upward (see below).

As with Classic, only your **current best result per lift** counts toward
the leaderboard — a new set that beats your previous best replaces it, and a
non-improving set still saves to your history without moving your points.
Your total challenge score is the sum of your best points across every lift.

Your performance card shows a progress bar toward the full target (e.g.
"12/20 reps → 6 pts") once the weight gate is met. Before the weight gate is
met, the card shows a weight-gate message instead (e.g. "Add 5 lb to start
scoring on Dip") — reps don't help you here, since weight is a strict
prerequisite. The same "Close to goal" and "Final stretch" highlights
described above apply to Rep Target cards too, tuned by the same
`CHALLENGES_CLOSE_TO_GOAL_*`/`CHALLENGES_ENDGAME_*` settings — a reps-based
closeness measure standing in for Classic's weight-based one once the gate
is cleared.

## Related pages

- [Units & added-weight lifts](bodyweight-and-units.md) — how lifts like
  pull-ups and dips (where you add or remove weight from your bodyweight)
  get scored, and how weights are displayed in your preferred unit.
- [Keeping your data fresh](sync-and-data-freshness.md) — how your logged
  sets make their way into the app, and why scoring happens as its own
  separate step.
