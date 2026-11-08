# keel_seo.playstore: Google Play keyword gaps

For a seed topic, finds the phrases people type into Google Play's search box,
measures the apps Play ranks for each one, and ranks the phrases where demand is
answered by weak apps. Keyless, no model call, a few hundred requests per seed.

## Run it

```
pip install 'keel-seo[playstore]'        # google-play-scraper + openpyxl
python -m keel_seo.playstore --seed "prop firm" --out <dir>
python -m keel_seo.playstore --seed "aquarium" --seed "fish tank" --gl gb --out <dir>
python -m keel_seo.playstore --name prop --brands brands.txt \
    --genres "Finance,Education,Tools,Productivity,Business" --seed "prop firm" --out <dir>
```

Writes `<seed>_playstore_<gl>_<date>.json`, `.csv` and `.xlsx` into `<dir>` (`--name` changes the stem), plus
`.playstore-cache.jsonl`, which makes a rerun or a resumed run free. Keyword data
never goes to git: point `--out` at a gitignored folder.

## What it does

1. **Expand.** Play's autocomplete (the store's own `batchexecute` RPC `IJ4APc`)
   is asked for the seed, then `seed a` to `seed 9`. Every new phrase is asked
   again; a full answer (five phrases) earns its own alphabet, capped by `--drill`.
2. **Measure.** Each phrase is searched; the top `--top` apps (default 10) get
   their details read: real installs, rating, rating count, last update, in-app
   price range. When the parser drops the update date, it is read from the page.
3. **Score.** Only apps whose title and short description name half the phrase
   count as relevant; unrelated giants that fill thin result pages are excluded
   from demand. See `score.py` for the formula and its weights.
4. **App score.** Each phrase also gets a builder's score (`ideas.py`): 35%
   demand, 30% weakness, 35% monetization (do the relevant apps sell anything,
   and does one with real reach). A brand query is cut to a quarter, and the
   verdict column says `build candidate`, `worth a look`, `weak`, a brand query,
   or `unserved, judge by hand`. Phrases Play answered with mostly the same apps
   are grouped into one idea. With `--genres`, a phrase whose top apps are
   mostly in other Play categories is marked `off topic` and left out: measured
   2026-11-08, "trading simulator" drifted into car games and "risk calculator"
   into medical apps.
5. **Complaints.** The 1 to 3 star reviews of weak, installed, relevant apps
   behind the best phrases are collected, and their repeated two-word phrases
   counted. Reading them is a person's job.

## Workbook sheets

| Sheet | One row per |
|---|---|
| Ideas | app idea (phrases sharing the same top apps), ranked by app score |
| Keywords | phrase, ranked by app score, with its verdict, reasons and top apps |
| Apps | app seen in any top list, with every phrase it ranks for |
| Complaint terms | repeated two-word phrase in one app's low-star reviews |
| Complaints | low-star review, most helpful first |
| Run | the parameters, counts and the caveat |

## The caveat

Google Play publishes no search volume. Demand is read from the installs of the
relevant apps, so a phrase that no app answers is marked `unserved` with unknown
demand rather than scored. Opportunity ranks phrases within one run; it is not a
forecast of downloads or revenue.
