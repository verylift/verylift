# Keeping your data fresh

very lift pulls your workouts from Liftosaur and scores them
against your challenges. This page explains where that data lives, when a
pull actually happens, and why a workout you just logged might not show up
on the leaderboard right away.

## One shared workout history

Everything you log gets pulled into a single history that belongs to you —
not to any one challenge. If you're in three challenges at once, they
all draw from that same history rather than each keeping their own copy.
Re-pulling a workout you've already synced just updates it in place; it
never creates a duplicate.

Because the history is shared, one pull refreshes it for every challenge
you're in at once — there's no need for each challenge to fetch your data
separately.

## Pulling data and scoring it are two separate steps

This is the most important thing to understand about how freshness works:
**getting your workouts into the app, and turning them into points, are two
different things that happen at different times.**

- **Pulling** fetches your recent workouts from Liftosaur and adds them to
  your shared history. It doesn't touch scoring or know anything about
  which challenges you're in.
- **Scoring** looks at your shared history and works out your points for one
  specific challenge. It never talks to Liftosaur — it only works with
  data that's already been pulled in.

Because scoring never has to call out to Liftosaur, the app can safely
re-check your standing every time you open a challenge page, without
worrying about hammering an external service or double-counting a set
you've already been credited for.

## Why pulls are throttled

To avoid pulling from Liftosaur too often, each person's pulls are limited
to roughly once every **10 minutes**. If you've had a successful pull in the
last 10 minutes, opening another challenge page won't trigger a fresh
pull — it'll just re-score whatever is already in your history.

This limit applies to you as a person, not to each challenge separately —
so a pull triggered from one challenge keeps every challenge you're in
up to date, and none of them need their own redundant pull right after.

## Your first pull goes back a year

The very first time you're synced, the app reaches back a full year to
build up your starting history, so you're not starting from a blank slate.
This runs in the background right after you sign up, so it doesn't hold up
your onboarding.

After that first big pull, every following pull is incremental — it only
looks for workouts newer than what's already in your history, so it stays
fast no matter how much history has built up.

## Why a workout you just logged might not show up yet

If you log a set in Liftosaur and don't see it reflected right away, it's
usually one of these:

- **You're within the 10-minute cooldown.** If you had a pull recently, the
  next page view won't trigger a new one — your set shows up the next time
  a pull actually runs.
- **A pull hasn't happened yet, so there's nothing new to score.** Scoring
  only works with what's already been pulled in; if your set isn't in your
  history yet, there's nothing new for scoring to find.
- **The challenge has already ended or been cancelled.** A completed or
  cancelled challenge stops pulling in new data entirely — see
  [Running a challenge](challenges.md) for what happens when a
  challenge wraps up.

In short: your new set becomes visible the next time a pull runs (once the
cooldown has passed), and the leaderboard catches up the next time you open
the challenge after that.

## Importing a workout CSV is a one-shot action, not a sync

If your tracker app doesn't offer very lift a direct sync — its API is
paywalled, or it simply isn't integrated yet — you can often still get your
data in by uploading the CSV file the app exports for you. One upload
control on the settings page accepts CSV exports from any supported tracker
app: the app is detected automatically from the file itself, so there's
nothing to pick from a list. Hevy is the first supported format.

Whichever app produced the file, an upload pools whatever working sets are
in it and immediately rescores every active challenge you're in — this is
closer to logging a lift manually than it is to a Liftosaur sync: there's no
cooldown, no watermark, and no background pull. Each upload is a complete,
explicit action — nothing happens with that data until you upload a file,
and re-uploading the same file is safe (it updates your existing history in
place rather than creating duplicates).

If you upload a CSV from a tracker app that isn't recognized yet, the app
tells you so rather than silently importing nothing or guessing at the
format.

## Related pages

- [How scoring works](scoring.md) — what happens to a workout once it's
  been pulled into your history.
- [Units & added-weight lifts](bodyweight-and-units.md) — how added-weight
  lifts like pull-ups and dips are shown and scored, and how weights are
  displayed in your preferred unit.
