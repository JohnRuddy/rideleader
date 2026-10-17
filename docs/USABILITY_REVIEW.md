# Ride Leader Kit: usability review

Reviewed 11 Oct 2026 against the running app (desktop, 390 px phone, real Strava route, own Overpass/BRouter). This is
a desk review by the builder, not a test with real ride leaders. Section 5 lists what only real leaders can settle.

## 1. Who this is for and what they actually do

A club ride leader is a volunteer. They are not a route-planning enthusiast and they do this in the gaps of a week.

| When | Where | What they need to know |
|---|---|---|
| Mid-week, evening | Laptop or phone on the sofa | Which route, which day, will it be dry, is there a café, is any road shut |
| Friday night / Saturday morning | Phone | The final yes/no, then one message to the WhatsApp group |
| At the meeting point | Phone in a back pocket, gloves on, maybe rain | Times, the plan, who is in which group, nearest AED |
| On the road | Garmin / Wahoo / Karoo | Turns, the café, where to regroup after the climb |
| After a bad day | Anywhere | "Did I do what a reasonable leader would have done?" (weather warning, daylight, closures) |

Constraints that follow from that:

1. **Mostly phone, often outdoors.** Glare, wet fingers, gloves: big targets, high contrast, no hover-only controls.
2. **Time-poor.** The first screen must answer "is this ride OK?", not show a form.
3. **Low tolerance for jargon.** "Corridor", "candidates", "waypoints" mean nothing. Spin, bunch, regroup, café stop do.
4. **Trust matters more than polish.** A wrong "all clear" is worse than "couldn't check".
5. **Everything ends in WhatsApp and a head unit.** Share text and exported files are the product; the web page is the means.

## 2. Findings

Severity: **High** stops a leader doing the job, **Med** costs time or causes mistakes, **Low** polish.

| # | Sev | Finding | Why it matters | Status |
|---|---|---|---|---|
| 1 | High | On a phone the stops panel (pick stops, Export) sat at the very bottom of a 5,000 px page, under weather, closures and sharing. | The core task was a long scroll away. | **Fixed.** Panel now follows the map and profile on phones; fixed bottom bar jumps to Checks / Day / Stops / Export / Share. |
| 2 | High | No single answer to "is this ride on?". Weather, warnings, closures, daylight, café hours lived in separate cards. | Leaders would have to read six cards to decide. | **Fixed.** "Ready to ride?" card at the top: red / amber / unknown / fine, each row says what it found and links to the fix. |
| 3 | High | A ride date that had passed was shown as normal on the route page, ride sheet and home list. | Easy to send the group a plan for yesterday. | **Fixed.** Flagged red in the checks and on the home list. |
| 4 | High | Closure check reported a footway under construction as a closure and "closed to bicycles" on paths. | False alarms train people to ignore the card. | **Fixed.** Paths, footways, steps, cycleways are ignored. Closure matching is also much faster (the whole ready-check went from 24 s to about 1 s). |
| 5 | High | Stop search and route editing depended on public servers that were unreachable or slow. | The app looked broken. | **Fixed.** Own Overpass and BRouter; search ~3-10 s. |
| 6 | Med | Strava routes arrive named by date ("02.09.2017"). No way to rename. | The group sees a meaningless title on the sheet and WhatsApp text. | **Fixed.** Rename on the route page. |
| 7 | Med | Home list showed distance only. | Can't tell which route is this Saturday's. | **Fixed.** Source badge and ride date/time, passed dates in red. |
| 8 | Med | Row action buttons in the stops list were emoji, which render differently per device and read as clip-art. | Inconsistent and easy to misread in glare. | **Fixed.** Drawn line icons with the same tooltips. |
| 9 | Med | "Nearest defibrillator per 10 km" was only reachable by knowing the "one per km" control. | Leaders want a sensible default, not a control panel. | **Open.** Recommend a one-press "sensible stops" preset. Not built: needs a decision on what "sensible" is for the club. |
| 10 | Med | Café opening hours are mostly missing in OpenStreetMap. | "Open on arrival" can't help when there is no data. | **Partly.** Checks say "ring ahead" and name the café. Needs leaders to correct hours (clock button) and the club library to fill up. |
| 11 | Med | Meeting point defaults to "Route start". | Riders get a map pin at the first GPS point, not "the car park at the Curragh". | **Flagged.** Shown as a check. Still a free-text field; no place search yet. |
| 12 | Med | Pace groups default to one group at 28 km/h. | Most clubs run A/B/C. | **Open.** Add club-level default groups in the club settings. |
| 13 | Low | Weather uses emoji glyphs. | Fine functionally; look varies by device. | Left. Replace with drawn icons if it bothers anyone. |
| 14 | Low | Only English; km only. | Fine for Irish clubs. | Left. |
| 15 | Low | Public ride sheet intentionally ships no JavaScript. | Means no live checks on the shared page; good for reliability. | By design. |

## 3. What changed in this pass

- "Ready to ride?" card (`app/readiness.py`, `/api/routes/<id>/readiness`) built from the same data as the ride sheet.
- Phone layout: stops panel straight after map and profile; fixed action bar.
- Rename a ride; home list with source and ride date; past dates flagged.
- Closure matching: vectorised and no longer treats paths as roads (ready-check 24 s to about 1.3 s cold, 0.13 s cached).
- Full re-skin (`app/static/theme.css`): early-90s lifeguard-tower look. Red, yellow, navy, sand; heavy italic
  headlines; hard shadows; lifebuoy mark. Contrast kept high for outdoor use. Night-patrol dark mode.
- Strava: "View on Strava" link on imported routes in the wording and styling Strava's brand rules require.

## 4. Strava brand and API compliance

From developers.strava.com/guidelines (revised 29 Sep 2025):

| Rule | State |
|---|---|
| Use the official "Connect with Strava" button (48 px high, orange or white), linking to the Strava OAuth URL | **Pending**: official artwork not yet installed; text button in use |
| "Powered by Strava" / "Compatible with Strava" logo allowed; must not be more prominent than the app name | **Pending**: official artwork not yet installed; text line in use, small |
| Never modify or animate the logos, never use them as the app icon | The lifebuoy is the app mark; no Strava artwork is altered |
| Link back to Strava data as "View on Strava", bold, underlined or #FC5200 | **Done** |
| Don't use "Strava" in the app name | **Done**: app is "Ride Leader Kit" |

## 5. What only real leaders can tell us

Test with five leaders from two clubs, each given one task and no instructions:

1. "You're leading Sunday. Get this route onto your Garmin."
2. "Is Sunday's ride on? Tell me how you decided."
3. "Send the group the plan."
4. "Add the café stop and tell me what time you reach it."

Watch for: where they scroll past something, what they mistake for a button, and which words they say out loud.
Things to measure: time to first export, number of taps to the WhatsApp text, any place they ask "what does this mean".

Known unknowns: device behaviour of exported cues on real Garmin, Wahoo and Karoo units; how the page reads in direct sun;
whether "Fix / Check / Unknown" is the right vocabulary.


## 6. Three refinement passes (design and function)

Direction after the first review: the 90s lifeguard theme was fun but read as a costume. The brief became "modern, trusted,
familiar to cyclists". The idea is the printed **roadbook**: paper and ink, one yellow for "this is the one", the magenta
course line a head unit draws, big tabular numerals, condensed road-sign type (Bahnschrift / DIN-style faces already on the
device, so no font downloads and no tracking).

### Pass 1: structure and system
Critique: the route page was still a long stack of twelve cards on every screen size; the title card, readiness card,
warnings, edit bar and map competed; "Fix" and "Check" colours were fighting a heavy decorative theme.

- Route page rebuilt as a **workbench**: map and elevation profile on one side (sticky on desktop), tools on the other in
  four tabs: **Checks** (readiness, warnings, day, ride plan), **Stops**, **Conditions** (weather, closures, climbs), **Share**.
  The last tab used is remembered per route; a status dot on Checks and a count on Stops.
- New design system: ink/paper/yellow tokens, condensed display type with tabular numerals, 12 px radii, 1 px hairlines,
  focus rings that read on any background, switches instead of checkboxes for view options.
- Stat strip (distance, climbing, descent, stops on device) as the page's headline numbers.
- Found and fixed a layout bug the redesign exposed: one long line widened the whole tools column (grid track shrink).

### Pass 2: detail and information
Critique: profile was colour only, no sense of "where are the climbs"; no feel for how full the head unit is; empty and
first-run states were plain text; pace groups needed three taps to be sensible; upload was a bare file input.

- Elevation profile gets the numbered category flags a stage profile has.
- Device meter (stops against the unit's course-point limit, amber at 80 %, red over) and a chip summary of what is ticked.
- Designed empty state for "no stops yet"; drop zone for GPX with drag-and-drop; A / B / C group preset (28 / 24 / 20 km/h).
- Readiness card loads with a skeleton instead of a spinner.

### Pass 3: the loop around the page
Critique: home had no idea what "next" is; the Strava notice cost three lines of a phone's first screen; four tabs did not
fit 390 px; the map hint talked about a right mouse button to people holding a phone.

- Home: **Up next** card (date block, countdown, meeting point), upcoming rides sorted first, past ones flagged.
- Strava notice folded into one line with "View on Strava" kept visible (brand rule).
- Tabs share the width on a phone; hint text follows the pointer type; toggles shortened.
- Contrast measured on every colour pair used for text and status (light and dark): lowest is 4.68:1, most above 7:1.
- Phone route page: about 3,000 px tall where it was about 5,900, with no sideways scroll.

### Still not verified
- Real ride leaders using it (section 5 above). Everything here is my judgement and screenshots, not observed behaviour.
- Bahnschrift is a Windows font; on Mac and Android the DIN-style fallbacks are used and look slightly different.
- Printing of the ride sheet and cue card was not re-checked after the restyle.
