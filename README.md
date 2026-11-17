# Fajna Clinic for Home Assistant

The patient account of [Файна Клініка](https://fajna.clinic) in Home Assistant: the visits
you have booked, as a calendar and sensors, for every patient card of every account you add.

The clinic has no API. The integration signs in the way the site's own form does and reads
the account page, so a redesign of the site can break it until the parser is updated.

## Install

HACS → ⋮ → Custom repositories → this repository, category *Integration* → install
**Fajna Clinic** → restart Home Assistant.

## Add an account

Settings → Devices & services → Add integration → **Fajna Clinic**, then the site's own
steps: phone number, the code the clinic texts, the account password. The password is sent
to the clinic once and is not stored; what is kept is the session cookie.

Add the integration again for another account. Every patient card of an account becomes a
device of its own.

When the clinic ends the session, Home Assistant shows a *re-authentication* notice: signing
in again needs a fresh SMS code, so it cannot happen without you.

## What you get, per patient

| Entity | |
| --- | --- |
| `calendar.<patient>_visits` | the booked visits: service as the title, doctor and instructions in the description. The page gives a start time only, so each event is one hour long |
| `sensor.<patient>_next_visit` | when the next visit starts; its service, doctor and instructions as attributes |
| `sensor.<patient>_upcoming_visits` | how many visits are booked |

The account is read every 30 minutes.

## Google Calendar

Every booked visit of every account becomes an event in one Google Calendar, titled
`[Іван] Масаж спини` — the patient's first name, from the card, then the service. The
people you list are invited to each event **without an email**, so it shows in their
calendars as well. The event is one: a reschedule or a cancellation reaches everybody's
calendar at once.

- **Kept in step every 30 minutes.** A new visit adds an event, a changed one updates it, a
  visit cancelled before it starts removes it. A visit that took place keeps its event.
- **Only its own events.** Each carries the visit's id in private properties, and nothing
  else in the calendar is read or touched. Reinstalling creates no duplicates.
- **Never on a failed read.** When the clinic's site is down or the session has ended,
  the calendar is left as it is.

### Set it up

1. In [Google Cloud](https://console.cloud.google.com/), in a project of yours (the one
   your Google Drive integration uses will do): enable the **Google Calendar API**, and if
   the OAuth client does not have it yet, add the redirect URI
   `https://my.home-assistant.io/redirect/oauth`.
2. **Publish the consent screen** (OAuth consent screen → *Publish app*). An app left in
   *Testing* gets refresh tokens that Google revokes after 7 days, and the link dies every
   week. A published app that is not verified only shows an «unverified app» warning to you.
3. Home Assistant → Settings → Devices & services → Add integration → **Fajna Clinic** →
   **Connect Google Calendar**. Enter the client ID and secret when asked, sign in with the
   Google account whose calendar should hold the visits, then choose the calendar
   (`primary` is its main one) and whom to invite.

The calendar and the invitees can be changed later in the entry's options.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

`tests/fixtures/` holds pages shaped like the real ones with made-up people. Never commit a
real page: it is somebody's medical record. `prototype/` is the probe the integration was
designed from; what it saves goes to `prototype/out/`, which git ignores.
