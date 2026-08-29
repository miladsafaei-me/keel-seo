# Search Console API setup — one service account, many properties

Everything `keel_seo.gsc` does runs on **one Google Cloud service account** whose email
is added as a user of each Search Console property. There is no per-project OAuth
flow, no browser consent, and no token to refresh: a service-account key mints
credentials for whichever scope a call needs.

Setup spans two different consoles, and almost every failure is a step missed in one of
them:

| What | Where | Once per |
|---|---|---|
| Create the Cloud project | console.cloud.google.com | account |
| Create the service account + JSON key | console.cloud.google.com | account |
| Enable **Search Console API** | console.cloud.google.com | Cloud project |
| Enable **Web Search Indexing API** | console.cloud.google.com | Cloud project |
| Add the service-account email as a property user | search.google.com/search-console | property |
| Verify property ownership | search.google.com/search-console | property |

The two API enablements are per **Cloud project**, not per site — enable them once and
every property served by that key inherits them. The permission grant is per
**property** and must be repeated for each site.

## 1. Cloud console — the project, the APIs, the key

1. Open <https://console.cloud.google.com/> and pick (or create) the project that will
   own this key. One project for all sites is correct: the key is shared, and so are
   the quotas.
2. Enable both APIs, in this project:
   - <https://console.cloud.google.com/apis/library/searchconsole.googleapis.com>
   - <https://console.cloud.google.com/apis/library/indexing.googleapis.com>

   Press **Enable** on each. Propagation takes a few minutes; a call made too early
   fails with `SERVICE_DISABLED`, naming the API it is missing.
3. **IAM & Admin → Service Accounts → Create service account.** A name is all it
   needs. Grant it **no** IAM roles — Search Console permission is granted in Search
   Console, not through Cloud IAM, and an IAM role here buys nothing.
4. On the new account: **Keys → Add key → Create new key → JSON**. The file downloads
   once and cannot be re-downloaded; lose it and you issue a new one.
5. Store it outside any repository, readable only by its owner:

   ```
   mkdir -p ~/.config/keel-seo
   mv ~/Downloads/<project>-<hash>.json ~/.config/keel-seo/gsc-service-account.json
   chmod 600 ~/.config/keel-seo/gsc-service-account.json
   ```

Confirm which identity the key carries:

```
python -m keel_seo.gsc check
```

The first line prints the service-account email and its Cloud project. That email is
what step 2 pastes into Search Console.

## 2. Search Console — grant the account access to each property

For **every** property, in <https://search.google.com/search-console>:

1. Select the property.
2. **Settings → Users and permissions → Add user.**
3. Paste the service-account email (it looks like
   `something@your-project.iam.gserviceaccount.com`).
4. Choose the permission level — and this is the step that decides which capabilities
   work:

   | Permission | Unlocks |
   |---|---|
   | Restricted | Search Analytics, reading sitemaps |
   | Full | the above **+ URL Inspection**, submitting/deleting sitemaps |
   | **Owner** | the above **+ the Indexing API** (`URL_UPDATED` / `URL_DELETED`) |

   Grant **Owner** unless there is a reason not to: it is the only level that unlocks
   everything, and the alternative is discovering the ceiling later as a 403.

A property must also be **verified** before any of this returns data. Domain properties
(`sc-domain:example.com`) verify with a DNS TXT record; URL-prefix properties verify
with an HTML file, a meta tag, or Google Analytics. Verification has no API — it is a
one-time browser step.

## 3. Prove it

```
python -m keel_seo.gsc check --site sc-domain:example.com
```

Eight checks run, each the cheapest real call that proves a capability, and each
failure prints the console to go fix:

```
[PASS] service-account key           gsc@proj.iam.gserviceaccount.com (project proj)
[PASS] property configured           sc-domain:example.com
[PASS] Search Console API reachable  5 propert(ies) visible to this key
[PASS] property permission           siteFullUser
[PASS] Search Analytics              query returned
[PASS] URL Inspection                https://example.com/ -> PASS / Submitted and indexed
[PASS] Sitemaps (read)               4 sitemap(s) registered
[FAIL] Indexing API                  ... SERVICE_DISABLED ...
       fix: Needs indexing.googleapis.com enabled AND Owner permission on the property
```

Inside a Django project the same checks run with the project's settings loaded, so they
also validate what the host configured rather than only what the shell exports:

```
python manage.py keel_seo_gsc_check
```

## Reading a failure

| Symptom | Cause | Fix |
|---|---|---|
| `SERVICE_DISABLED` | the API is off on the Cloud project | enable it, wait a few minutes |
| 403 on every call | the account is not a user of the property | add its email in Search Console |
| 403 on inspection only | permission is Restricted | raise it to Full or Owner |
| 403 on the Indexing API only | permission is Full | raise it to Owner |
| 404 on a property | the property string does not match | `python -m keel_seo.gsc sites list` |
| 429 | quota | inspection 2,000/day per property; indexing 200/day per Cloud project |
| connection reset, no status | network, not API | retried automatically; if it persists, run from a server |

`sites list` is the fastest disambiguation: it prints the exact property strings this
key can act on and the permission level held on each. A Domain property is
`sc-domain:example.com` — no scheme, no trailing slash — and a URL-prefix property is
the full origin with its trailing slash.

## What has no API at all

Three things in Search Console cannot be automated, and each one is a browser step:

- **Users and permissions.** Adding a service account to a property, or changing its
  level, has no API — which is why the setup above is a manual step per property.
- **Property verification.** `sites.add` registers a property; verifying ownership
  (the DNS TXT record, or an HTML file / meta tag) is done in the browser.
- **The Removals tool.** The temporary ~6-month block is UI-only. The Indexing API's
  `URL_DELETED` is a different thing — a crawl hint, not a removal.

One more, for completeness: `searchconsole v1` still lists a `urlTestingTools`
resource (the Mobile-Friendly Test), but Google retired it in December 2023 and every
call now answers `400 Request contains an invalid argument`. It is intentionally not
wrapped.

## Serving several properties from one key

Nothing needs to be duplicated in Cloud: one project, one service account, one key,
with its email added to each property. What varies per site is only which property a
given call targets, resolved in this order:

1. an explicit `--site` / `site=` argument,
2. `$GSC_SITE`,
3. Django `KEEL_SEO["gsc_site"]`.

So a container sets `GSC_SITE` for its own site and mounts the shared key at
`GSC_CREDENTIALS`, while an operator on a laptop overrides both per command.

Two limits are worth knowing before spreading a key across sites: **URL Inspection
quota is per property** (2,000/day each, so sites do not compete), but **Indexing API
quota is per Cloud project** (200 publishes/day total, shared by every site on the
key). If several sites need heavy indexing submission, give them separate Cloud
projects — separate keys, same Search Console grants — rather than separate service
accounts inside one project, because the quota follows the project.
