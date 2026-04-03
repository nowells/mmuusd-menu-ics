#!/usr/bin/env python3
"""
Fetch school meal menus from the LinqConnect (formerly Titan Schools) API
and produce an iCalendar (.ics) feed.

Usage:
    python generate_ical.py                          # uses defaults / env vars
    python generate_ical.py --building-id X --district-id Y
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from hashlib import sha256

import requests
from icalendar import Calendar, Event

# ── defaults (override via env vars or CLI flags) ──────────────────────────
DEFAULT_BUILDING_ID = "930a6424-06f4-ed11-84af-fa270f46f553"
DEFAULT_DISTRICT_ID = "d9bd5361-fbf3-ed11-84ae-8f8c34831c13"
API_BASE = "https://api.linqconnect.com/api/FamilyMenu"
LOOKBACK_DAYS = 14       # include recent past so the calendar isn't empty if you subscribe mid-week
LOOKAHEAD_DAYS = 90      # ~3 months ahead — the API usually only has data a few weeks out
OUTPUT_FILE = "school-menu.ics"


def parse_args():
    p = argparse.ArgumentParser(description="LinqConnect → iCal converter")
    p.add_argument("--building-id", default=os.getenv("BUILDING_ID", DEFAULT_BUILDING_ID))
    p.add_argument("--district-id", default=os.getenv("DISTRICT_ID", DEFAULT_DISTRICT_ID))
    p.add_argument("--output", default=os.getenv("OUTPUT_FILE", OUTPUT_FILE))
    p.add_argument("--lookback", type=int, default=int(os.getenv("LOOKBACK_DAYS", LOOKBACK_DAYS)))
    p.add_argument("--lookahead", type=int, default=int(os.getenv("LOOKAHEAD_DAYS", LOOKAHEAD_DAYS)))
    return p.parse_args()


def fmt_date(d: date) -> str:
    """Format a date the way the LinqConnect API expects: m-d-YYYY."""
    return f"{d.month}-{d.day}-{d.year}"


def fetch_menu(building_id: str, district_id: str, start: date, end: date) -> dict:
    """Call the FamilyMenu endpoint and return parsed JSON."""
    params = {
        "buildingId": building_id,
        "districtId": district_id,
        "startDate": fmt_date(start),
        "endDate": fmt_date(end),
    }
    print(f"  Fetching {API_BASE}  {fmt_date(start)} → {fmt_date(end)} …")
    resp = requests.get(API_BASE, params=params, timeout=60, headers={"Accept": "application/json", "User-Agent": "LinqConnect Menu iCal Generator/1.0"})
    resp.raise_for_status()
    return resp.json()


def extract_meals(data: dict) -> list[dict]:
    """
    Walk the API response and return a flat list of dicts:
        { "date": date, "session": "Lunch"|"Breakfast", "items": ["ITEM A", …] }

    The API nests data as:
        FamilyMenuSessions[]
          .ServingSession          ("Breakfast" | "Lunch" | …)
          .MenuPlans[]
            .Days[]
              .Date                ("1/18/2023")
              --- newer format ---
              .MenuMeals[]
                .RecipeCategories[]
                  .CategoryName
                  .Recipes[].RecipeName
              --- older format ---
              .RecipeCategories[]
                .CategoryName
                .Recipes[].RecipeName
    """
    meals: list[dict] = []

    for session in data.get("FamilyMenuSessions", []):
        session_name = session.get("ServingSession", "Meal")
        # Normalize to "Breakfast" or "Lunch"
        if "breakfast" in session_name.lower():
            session_label = "Breakfast"
        elif "lunch" in session_name.lower():
            session_label = "Lunch"
        elif "dinner" in session_name.lower():
            session_label = "Dinner"
        elif "snack" in session_name.lower():
            session_label = "Snack"
        else:
            session_label = session_name

        for plan in session.get("MenuPlans", []):
            for day in plan.get("Days", []):
                raw_date = day.get("Date", "")
                try:
                    meal_date = datetime.strptime(raw_date, "%m/%d/%Y").date()
                except ValueError:
                    try:
                        meal_date = datetime.strptime(raw_date, "%Y-%m-%dT%H:%M:%S").date()
                    except ValueError:
                        print(f"    ⚠ skipping unparseable date: {raw_date}")
                        continue

                recipe_names: list[str] = []

                # ── newer format: Days[].MenuMeals[].RecipeCategories[] ──
                for menu_meal in day.get("MenuMeals", []):
                    for cat in menu_meal.get("RecipeCategories", []):
                        for recipe in cat.get("Recipes", []):
                            name = recipe.get("RecipeName", "").strip()
                            if name:
                                recipe_names.append(name)

                # ── older format: Days[].RecipeCategories[] directly ──
                if not recipe_names:
                    for cat in day.get("RecipeCategories", []):
                        for recipe in cat.get("Recipes", []):
                            name = recipe.get("RecipeName", "").strip()
                            if name:
                                recipe_names.append(name)

                if recipe_names:
                    meals.append({
                        "date": meal_date,
                        "session": session_label,
                        "items": recipe_names,
                    })

    return meals


def build_calendar(meals: list[dict]) -> Calendar:
    """Build an iCalendar object from the extracted meals."""
    cal = Calendar()
    cal.add("prodid", "-//LinqConnect Menu//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "School Menu")
    cal.add("x-wr-caldesc", "Daily school meal menu from LinqConnect")
    cal.add("method", "PUBLISH")
    # Refresh every 12 hours when used as a subscription
    cal.add("x-published-ttl", "PT12H")

    for meal in meals:
        ev = Event()
        summary = f"🍽 {meal['session']}"
        description = "\n".join(f"• {item}" for item in meal["items"])
        short_list = ", ".join(meal["items"][:5])
        if len(meal["items"]) > 5:
            short_list += " …"

        ev.add("summary", f"{summary}: {short_list}")
        ev.add("description", description)
        ev.add("dtstart", meal["date"])  # all-day event
        ev.add("dtend", meal["date"] + timedelta(days=1))
        ev.add("dtstamp", datetime.utcnow())
        ev.add("transp", "TRANSPARENT")  # don't block the calendar

        # Stable UID so re-runs update rather than duplicate
        uid_seed = f"{meal['date'].isoformat()}-{meal['session']}"
        uid = sha256(uid_seed.encode()).hexdigest()[:16] + "@linqconnect-menu"
        ev.add("uid", uid)

        cal.add_component(ev)

    return cal


def main():
    args = parse_args()

    today = date.today()
    start = today - timedelta(days=args.lookback)
    end = today + timedelta(days=args.lookahead)

    print(f"LinqConnect → iCal  |  window {start} … {end}")
    data = fetch_menu(args.building_id, args.district_id, start, end)

    meals = extract_meals(data)
    print(f"  Extracted {len(meals)} meal events")

    if not meals:
        print("  ⚠ No meals found — writing an empty calendar anyway.")

    cal = build_calendar(meals)

    with open(args.output, "wb") as f:
        f.write(cal.to_ical())

    print(f"  ✓ Wrote {args.output}  ({os.path.getsize(args.output):,} bytes)")


if __name__ == "__main__":
    main()
