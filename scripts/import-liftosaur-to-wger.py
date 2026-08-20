#!/usr/bin/env python3
"""Import a Liftosaur workout history CSV export into a wger instance via its REST API.

Requires an API key from the target wger account (Settings -> API key in wger,
or /api/v2/token/ on older versions) and the `wger-api-client` package:

    pip install wger-api-client

Usage:
    python3 import-liftosaur-to-wger.py history.csv \\
        --base-url https://wger.example.com \\
        --api-key <key>

Or keep the key out of your shell history / process list by putting it in a
.env file (WGER_API_KEY=...) next to the script, or exporting it:

    export WGER_API_KEY=<key>
    python3 import-liftosaur-to-wger.py history.csv --base-url https://wger.example.com
"""

import argparse
import csv
import datetime
import os
import sys
from collections import OrderedDict
from pathlib import Path

from wger_api_client import AuthenticatedClient
from wger_api_client.api.exercise import exercise_create
from wger_api_client.api.exercise_translation import exercise_translation_create
from wger_api_client.api.exercisecategory import exercisecategory_list
from wger_api_client.api.exerciseinfo import exerciseinfo_list
from wger_api_client.api.language import language_list
from wger_api_client.api.workoutlog import workoutlog_create
from wger_api_client.api.workoutsession import workoutsession_create
from wger_api_client.models.exercise_request import ExerciseRequest
from wger_api_client.models.exercise_translation_request import (
    ExerciseTranslationRequest,
)
from wger_api_client.models.workout_log_request import WorkoutLogRequest
from wger_api_client.models.workout_session_request import WorkoutSessionRequest

# Liftosaur exercise name -> exact wger exercise name, for cases where the
# names differ but refer to the same movement. Anything not listed here is
# looked up by exact name match first.
NAME_ALIASES = {
    "Arnold Press": "Arnold Shoulder Press",
    "Bent Over Row": "Bent Over Rowing",
    "Bulgarian Split Squat": "Bulgarian Split Squats",
    "Chest Dip": "Dips",
    "Deadlift": "Deadlifts",
    "Front Squat": "Front Squats",
    "Hanging Leg Raise": "Hanging Leg Raises",
    "Incline Bench Press": "Incline Bench Press - Barbell",
    "Lat Pulldown": "Lat Pulldown (Wide Grip)",
    "Pull Up": "Pull-ups",
    "Push Press, Barbell": "Push Press",
    "Push Up": "Push Ups",
    "Squat": "Barbell Squat",
}

# Liftosaur exercise name -> wger exercise category name, used only when no
# match (direct or aliased) exists on the server and a custom exercise needs
# to be created.
CUSTOM_EXERCISE_CATEGORIES = {
    "Behind The Neck Press": "Shoulders",
    "Bent Over One Arm Row": "Back",
    "Bent Over Row, Leverage Machine": "Back",
    "Chin Up, Leverage Machine": "Back",
    "Pendlay Row": "Back",
    "Pull Up, Leverage Machine": "Back",
    "Stair Machine Floors": "Cardio",
}


def load_api_key(args):
    if args.api_key:
        return args.api_key

    env_path = Path(args.env_file)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == args.env_var:
                return value.strip().strip('"').strip("'")

    key = os.environ.get(args.env_var)
    if key:
        return key

    sys.exit(
        f"No API key found. Pass --api-key, set {args.env_var} in "
        f"{args.env_file}, or export {args.env_var}."
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "csv_path", help="Path to the Liftosaur workout history CSV export"
    )
    parser.add_argument(
        "--base-url", required=True, help="Base URL of the wger instance"
    )
    parser.add_argument(
        "--api-key", help="wger API key (overrides --env-file/--env-var)"
    )
    parser.add_argument(
        "--env-file", default=".env", help="Path to a .env file containing the API key"
    )
    parser.add_argument(
        "--env-var",
        default="WGER_API_KEY",
        help="Variable name to read the API key from",
    )
    parser.add_argument(
        "--language-code",
        default="en",
        help="wger language code to match/create exercises in",
    )
    parser.add_argument(
        "--include-warmups",
        action="store_true",
        help="Also import warmup sets (skipped by default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the CSV and report what would be imported without writing anything",
    )
    return parser.parse_args()


def get_language_id(client, language_code):
    page = language_list.sync(client=client)
    for lang in page.results:
        if lang.short_name == language_code:
            return lang.id
    sys.exit(f"Language code {language_code!r} not found on the server")


def get_category_id(client, category_name, cache):
    if not cache:
        page = exercisecategory_list.sync(client=client)
        cache.update({c.name: c.id for c in page.results})
    if category_name not in cache:
        sys.exit(f"Exercise category {category_name!r} not found on the server")
    return cache[category_name]


def find_exercise_id_by_name(client, name, language_code):
    resp = exerciseinfo_list.sync(
        client=client, name_exact=name, language_code=language_code, limit=1
    )
    if resp and resp.results:
        return resp.results[0].id
    return None


def get_or_create_exercise(
    client, name, language_id, language_code, category_cache, exercise_cache
):
    if name in exercise_cache:
        return exercise_cache[name]

    lookup_name = NAME_ALIASES.get(name, name)
    found = find_exercise_id_by_name(client, lookup_name, language_code)
    if found:
        exercise_cache[name] = found
        return found

    category_name = CUSTOM_EXERCISE_CATEGORIES.get(name)
    if category_name is None:
        sys.exit(
            f"No match for exercise {name!r} and no entry in "
            "CUSTOM_EXERCISE_CATEGORIES to create it as a custom exercise. "
            "Add one of those mappings and re-run."
        )

    category_id = get_category_id(client, category_name, category_cache)
    exercise = exercise_create.sync(
        client=client,
        body=ExerciseRequest(
            category=category_id, license_author="import-liftosaur-to-wger"
        ),
    )
    exercise_translation_create.sync(
        client=client,
        body=ExerciseTranslationRequest(
            name=name,
            exercise=exercise.id,
            description_source="Imported from Liftosaur history.",
            language=language_id,
            license_author="import-liftosaur-to-wger",
        ),
    )
    print(f"  created custom exercise: {name!r} (category={category_name})")
    exercise_cache[name] = exercise.id
    return exercise.id


def group_sessions(rows):
    sessions = OrderedDict()
    for row in rows:
        sessions.setdefault(row["Workout DateTime"], []).append(row)
    return sessions


def working_sets_for(set_rows, include_warmups):
    return [
        r
        for r in set_rows
        if (include_warmups or r["Is Warmup Set?"] == "0")
        and r["Completed Reps"].strip() != ""
    ]


def main():
    args = parse_args()

    with open(args.csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    sessions = group_sessions(rows)

    client = None
    language_id = None
    if not args.dry_run:
        api_key = load_api_key(args)
        client = AuthenticatedClient(
            base_url=args.base_url, token=api_key, prefix="Token"
        )
        language_id = get_language_id(client, args.language_code)

    category_cache = {}
    exercise_cache = {}
    sessions_created = 0
    logs_created = 0
    skipped_empty = 0

    for dt_str, set_rows in sessions.items():
        sets = working_sets_for(set_rows, args.include_warmups)
        if not sets:
            skipped_empty += 1
            continue

        dt = datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        program = set_rows[0]["Program"]
        day_name = set_rows[0]["Day Name"]
        notes = f"{program} / {day_name}".strip(" /")

        if args.dry_run:
            print(f"[dry-run] {dt.date()} '{notes}': {len(sets)} sets")
            sessions_created += 1
            logs_created += len(sets)
            continue

        session = workoutsession_create.sync(
            client=client,
            body=WorkoutSessionRequest(
                date=dt.date(),
                notes=notes,
                impression="2",
                time_start=dt.time().isoformat(),
            ),
        )
        sessions_created += 1

        for r in sets:
            exercise_id = get_or_create_exercise(
                client,
                r["Exercise"],
                language_id,
                args.language_code,
                category_cache,
                exercise_cache,
            )
            reps = r["Completed Reps"].strip()
            weight_val = (
                r["Completed Weight Value"].strip()
                or r["Required Weight Value"].strip()
            )
            weight_unit_str = (
                r["Completed Weight Unit"].strip()
                or r["Required Weight Unit"].strip()
                or "lb"
            )
            weight_unit_id = 1 if weight_unit_str == "kg" else 2

            workoutlog_create.sync(
                client=client,
                body=WorkoutLogRequest(
                    exercise=exercise_id,
                    date=dt,
                    session=session.id,
                    repetitions_unit=1,
                    repetitions=reps,
                    weight_unit=weight_unit_id,
                    weight=weight_val or "0",
                ),
            )
            logs_created += 1

    mode = "would create" if args.dry_run else "created"
    print(
        f"\n{mode}: {sessions_created} sessions, {logs_created} logs; "
        f"skipped {skipped_empty} empty sessions"
    )


if __name__ == "__main__":
    main()
