# LinkedIn plugin — tool catalog

Active only on `*.linkedin.com`. No API key, no OAuth — reads the
user's currently-loaded LinkedIn page DOM via a browser primitive.

## Page-shape detector

| Page | URL | `page_type` |
|---|---|---|
| Feed / home | `/` or `/feed/...` | `feed` |
| Member profile | `/in/<id>` | `profile` |
| Company page | `/company/<slug>` | `company` |
| Job posting | `/jobs/view/<id>` | `job` |
| Jobs hub | `/jobs/...` | `jobs` |
| Messaging | `/messaging/...` | `messaging` |
| My Network | `/mynetwork/...` | `mynetwork` |
| Notifications | `/notifications/...` | `notifications` |
| Search results | `/search/...` | `search` |
| Anything else | — | `other` |

## Tools

### `linkedin_get_page_context`

Cheap probe. Returns `{url, page_type, title, profile_id,
company_slug, job_id, params}`.

### `linkedin_read_profile`

Full text of a member profile. Returns `{profile_id, sections,
empty_or_missing, truncated}` where `sections.main` is the top card +
About and every other key is the complete `/details/<section>/` list.
Defaults to the open profile; `profile_id` reads another. Default
sections: experience, education, skills, certifications, projects,
languages.

How it works: LinkedIn renders profiles client-side and lazily — the
SSR HTML from `fetch('/in/<id>/')` is chrome only. The primitive loads
each page in a same-origin iframe that sits on screen (near-invisible,
click-through; an offscreen frame never renders its sections), scrolls
it, and reads `innerText` once it stops growing. ~20–90 s total.

Use it for resumes, bios and candidate summaries. Do not reverse-engineer
LinkedIn's private APIs or JS bundles instead — that route has been
tried and burns the whole turn.
