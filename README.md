# Fajna Clinic for Home Assistant

The patient account of [Файна Клініка](https://fajna.clinic) in Home Assistant: the visits
you have booked, as a calendar and sensors, for the patient cards you choose of every account
you add.

The clinic has no API. The integration signs in the way the site's own form does and reads
the account page, so a redesign of the site can break it until the parser is updated.

## Install

HACS → ⋮ → Custom repositories → this repository, category *Integration* → install
**Fajna Clinic** → restart Home Assistant.

## Add an account

Settings → Devices & services → Add integration → **Fajna Clinic**, then the site's own
steps: phone number, the code the clinic texts, the account password. The password is sent
to the clinic and is not stored (unless you turn on [signing in by itself](#signing-in-again-by-itself));
what is kept is the session cookie.

One phone can hold several patient cards (a family's). Once signed in, you tick which of
them to add; an account with one card skips the question. Every chosen card becomes a device
of its own. The account's **Configure → Patient cards** changes the choice later: an unticked card is
removed with its entities and its Google Calendar events.

Add the integration again for another account.

When the clinic ends the session (it does so after about two days, however often the
account is read), Home Assistant shows a *re-authentication* notice: signing in again needs a
fresh SMS code. Afterwards the card choice comes up again with the cards already added ticked:
send it as it is to keep them.

### Signing in again by itself

Home Assistant can sign in again without you, if the clinic's SMS reaches it:

1. The account's **Configure → Automatic sign-in**: turn it on and enter the account password.
   It is kept in Home Assistant's storage (`.storage/core.config_entries`, next to the
   session) and goes with its backups. Turning the option off forgets it.
2. Pass the clinic's SMS to the `fajna_clinic.submit_sms` action. On an iPhone with the
   [Home Assistant app](https://apps.apple.com/app/home-assistant/id1099568401): Shortcuts →
   Automation → **New automation** → **Message** → *Message contains* a word the clinic's
   SMS always has (e.g. the clinic's name) → **Run Immediately** → action **Perform Action**
   (Home Assistant) → `fajna_clinic.submit_sms`, with `message` set to the message's
   *Content* (*Shortcut Input*).

When the session ends, the integration asks the clinic for a code, waits up to 5 minutes for
the SMS, then sends the code and the password. The entities are unavailable meanwhile and the
Google Calendars are left as they are. The code is the first group of 4–6 digits in the text;
an SMS that comes while nothing is signing in is ignored.

If no SMS comes in time, or the clinic refuses the code, the re-authentication notice comes
as before. A refused password is forgotten, so it is not tried again. It does not try more
than once an hour, and not again after a failure until a sign-in works: each try is an SMS.

Try the automation once before relying on it: sign in on the clinic's site, and the log
should say *An SMS code was passed on, but no account is signing in* — the SMS arrived. iOS
may sort a code from an unknown sender away from your messages; if nothing reaches Home
Assistant, add the sender to Contacts.

The log (`custom_components.fajna_clinic`, level *info*) says how long each session lived.

## What you get, per patient

| Entity | |
| --- | --- |
| `calendar.<patient>_visits` | the booked visits: service as the title, doctor and instructions in the description. The page gives a start time only, so each event is one hour long |
| `sensor.<patient>_next_visit` | when the next visit starts; its service, doctor and instructions as attributes |
| `sensor.<patient>_upcoming_visits` | how many visits are booked |

The account is read every 30 minutes.

## Google Calendar

Each patient can have a Google Calendar of their own: link one Google account per person,
and tick whose cards it holds (a parent's calendar can hold a child's visits too). A card
is in one calendar at most, so no visit is written twice. Every booked visit becomes an
event titled `[Іван] Масаж спини` — the patient's first name, from the card, then the
service. The people you list are invited to each event **without an email**, so it shows
in their calendars as well. The event is one: a reschedule or a cancellation reaches
everybody's calendar at once.

- **Linked guests accept at once.** When a guest has linked their own Google account here
  too, the integration accepts the invitation for them, in their main calendar: Valeria's
  massage with you invited shows in your calendar as *going*. An invitation the guest has
  answered themselves (declined, say) is left as they answered it.

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
   Google account whose calendar should hold the visits, then tick the patient cards, choose
   the calendar (`primary` is its main one) and whom to invite. Add the patient account
   first: the cards come from it.
4. Repeat step 3 for every person with a calendar of their own, signing in with their
   Google account. The same OAuth client serves them all.

The cards, the calendar and the invitees can be changed later in the entry's options.

A Google link made before 0.4 holds every card, as it did. To give someone a calendar of
their own, first untick their card in that link's options.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

`tests/fixtures/` holds pages shaped like the real ones with made-up people. Never commit a
real page: it is somebody's medical record. `prototype/` is the probe the integration was
designed from; what it saves goes to `prototype/out/`, which git ignores.
