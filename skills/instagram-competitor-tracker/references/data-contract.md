# Data contract

## What Upscrape returns

`instagram.profile.get` with `{"url": "<username>", "recent_posts_limit": 12}` costs 5 credits. The profile is in `results[0].data.data`. Fields the tracker reads:

| Field | Use |
| --- | --- |
| `username`, `full_name`, `is_verified`, `is_private` | Labels |
| `follower_count`, `following_count`, `media_count` | Profile numbers |
| `recent_posts[].code` | Post ID. The link is `https://www.instagram.com/p/<code>/` |
| `recent_posts[].taken_at` | Unix seconds |
| `recent_posts[].like_count`, `comment_count` | Engagement |
| `recent_posts[].media_type` | 1 photo, 2 video, 8 carousel |
| `recent_posts[].like_and_view_counts_disabled` | Likes hidden |
| `recent_posts[].owner.username` | The owner, when it isn't the account itself (a collab) |
| `recent_posts[].coauthor_producers[].username` | Co-authors |
| `recent_posts[].caption.text` | Caption, used to detect offers and shown in previews |

## The files to save for `ingest` (path B)

Preferred: the two `upscrape_get_job_result` answers from SKILL.md path B, saved exactly as returned, passed as `--profile` and `--posts`. Their keys are JSON Pointers (`"/data/follower_count"`, `"/code"`); `ingest` turns them back into the shape below.

A cut-off preview (`"result_complete": false`) is refused, because it usually holds only some of the posts.

`--file` also accepts any of these:

- the whole job,
- `results[0]`,
- `results[0].data`,
- or this minimal shape, which is the easiest to write after reading a job through MCP:

```json
{
  "data": {
    "username": "glossier",
    "full_name": "Glossier",
    "follower_count": 3114919,
    "following_count": 3618,
    "media_count": 6116,
    "is_verified": true,
    "is_private": false,
    "recent_posts": [
      {
        "code": "Df-IDgPG8W5",
        "taken_at": 1795193473,
        "like_count": 4947,
        "comment_count": 26,
        "media_type": 8,
        "like_and_view_counts_disabled": false,
        "owner": {"username": "glossier"},
        "coauthor_producers": [],
        "caption": {"text": "Some friends make the holidays happen..."}
      }
    ]
  }
}
```

`caption` may also be a plain string, and `owner` may be a plain username. Pass `--fetched-at` with the job's `completed_at` so that "fresh post" ages are right. Without it, the current time is used.

## The tracker folder

```
ig-tracker/
  accounts.json                     {"handles": [...], "you": "glossier", "brand": {...}}
  snapshots/YYYY-MM-DD/<handle>.json one per account per day: profile + posts, normalised
  dashboard.html  data.json  brief.md
```

Snapshots are plain JSON. To stop tracking an account, remove it from `accounts.json`; its history stays. To start over, delete the folder.

## data.json

The fields at the top level are `generated_at`, `first_day`, `last_day`, `days_tracked`, `window_days`, `fresh_hours`, `breakout_multiple`, `you`, `accounts[]`, `posts[]`, `highlights[]`, `notes[]` and `version`. Per-account and per-post fields are listed in `metrics.md`. The dashboard reads only this object, and `build` inlines it where the template says `/*__DATA__*/null`.

## brand (optional, in accounts.json and data.json)

Set with `track.py brand`. All keys are optional: `name` (prepared by), `client` (report title), `accent` (`#RRGGBB`), `logo` (a `data:image/...;base64,` URI, under 300 KB). The dashboard ignores an accent or logo that doesn't match these formats.

