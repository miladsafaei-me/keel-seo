# keel_seo.youtube

Competitor measurement on YouTube, keyless by default.

`keel_seo.keywords --ds yt` already harvests what people **type** into YouTube.
This package measures what they **watched**, which is the only external evidence
of how the recommendation surface behaved.

## Why two different measurements

A video reached through search was chosen by a query, so the title's phrasing is
the lever and a keyword universe predicts it. A video reached through the home
feed or the sidebar was chosen by a model optimising click-through and watch
time, and no keyword predicts it at all. The nearest measurable proxy is how far
a video ran past its own channel's normal — which is what `analysis.py`
computes, and why every multiple here is within a channel and never between two.

## The three commands

```bash
python -m keel_seo.youtube resolve  --channels channels.txt --out registry.json
python -m keel_seo.youtube snapshot --registry registry.json --store snapshots/
python -m keel_seo.youtube report   --registry registry.json --store snapshots/ --out report/
```

`resolve` runs once per new competitor. `snapshot` is the one that must repeat —
weekly is right. `report` reads whatever history exists.

## Two things measured on the way in, both of which would have shipped silent

**A handle is not a brand.** `youtube.com/@FTMO` resolves, answers HTTP 200, and
is a toy-unboxing channel called "An Toys TV"; FTMO is at `@FTMOcom`. So every
resolution carries the channel's own title back, and a registry line may state
the name it expects.

**A right title is not a live channel.** A brand often owns several channels and
posts to one. FundingPips owns three; `@fundingpipsofficial` has an empty feed
and `@fundingpipscom` is the live one, and both answer with the brand's name. So
`resolve` also probes the feed and warns on an empty one.

## The maturity window adapts, and says so

A baseline drawn from videos published yesterday measures how many subscribers
saw a notification. The default window excludes anything under 14 days — but
three of the first five competitors measured here post faster than 15 uploads
per 14 days, so that window matured none of their feed and they reported no
baseline at all. The window therefore narrows to keep the older half of a feed,
never below 2 days, and the chosen window is printed with every baseline. An
adaptive threshold that does not state what it adapted to is how two channels
end up compared against different rulers.

## The first run is worth doing; the tenth is worth much more

A feed reports a running view total, so one fetch can only say how a video did
over its whole life. Two fetches a week apart turn the same free endpoint into a
velocity measurement, and velocity — views gained per day, right now — is the
signal that says YouTube is pushing something this week. `store.py` never
overwrites for exactly this reason.

## `api.py` is optional and stays inside the free quota

The Data API adds the back catalogue, Shorts and subscriber counts, for a key
and no money. The trap is `search.list` at 100 units of a 10,000-unit day.
Nothing here calls it: a channel's uploads playlist id is its channel id with
`UC` swapped for `UU` (0 units), `playlistItems.list` walks it at 1 unit per 50,
and `videos.list` returns statistics at 1 unit per 50. Five channels at a
thousand videos each costs 200 units. The client counts what it spends.
