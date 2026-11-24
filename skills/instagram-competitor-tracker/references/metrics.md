# Metrics: exact formulas

`scripts/track.py build` implements these formulas. Follow them exactly when computing by hand (path C in SKILL.md), so the numbers match.

## Inputs per account

- `snapshots`: one per day, each holding the profile (`followers`, `following`, `media_count`) and the latest 12 posts.
- `now`: the newest `fetched_at` across all snapshots.
- `posts`: every post seen in any snapshot, keyed by `code`. If a post appears in several snapshots, the newest one wins.

## Per post

| Field | Formula |
| --- | --- |
| `engagement` | `likes + comments`. Null when the account hides likes. |
| `age_hours` | `(fetched_at of the snapshot that saw it - taken_at) / 3600` |
| `fresh` | `age_hours < 24`. Fresh posts are shown but kept out of every median. |
| `rate` | `engagement / followers * 100` (percent) |
| `multiple` | `engagement / median_engagement` of its own account |
| `breakout` | `multiple >= 3` and not fresh |
| `promo` | The caption matches the offer pattern (`PROMO_RE` in `track.py`): percent off, sale, discount, promo code, Black Friday, Cyber Monday or Week, giveaway, free gift, gift with purchase, sitewide, BOGO. |
| `format` | `media_type` 2 = video (reels included), 8 = carousel, 1 = photo, anything else = other |

## Per account

| Field | Formula |
| --- | --- |
| `scored` | Posts that are not fresh and have a non-null engagement. |
| `median_engagement` | The median of `engagement` over `scored`. |
| `median_likes`, `median_comments` | The medians over `scored`. |
| `engagement_rate` | `median_engagement / followers * 100`, latest followers |
| `recent` | Posts with `taken_at >= now - 28 days` |
| `posts_per_week` | `len(recent) / span_days * 7`, where `span_days = max(1, (now - max(oldest post seen, now - 28 days)) / 86400)`, rounded to 1 decimal place |
| `cadence_partial` | True when the oldest post seen is newer than 28 days ago. The dashboard then shows "from N posts". |
| `format_mix` | Count of `recent` by format |
| `format_engagement` | The median engagement of `scored` posts inside the 28-day window, by format |
| `promo_share` | `promo posts in recent / len(recent)` |
| `collabs` | Every co-author on a post, plus the post owner when the owner isn't the account. Each one lists its post codes. |
| `change_Nd` (N = 1, 7, 30) | Latest followers minus followers in the newest snapshot dated on or before `latest day - N`. Null when no such snapshot exists. |

## Highlights ("What stands out")

Generated in this order, at most one line per rule:

1. **Biggest breakout:** the post with the highest `multiple`.
2. **Engagement-rate gap:** the highest `engagement_rate` against the lowest, needing at least 2 accounts.
3. **Most frequent poster:** the account with the highest `posts_per_week`, with "(from its last N posts)" added while cadence is partial.
4. **Comment-heavy post:** the non-fresh post with the most comments per like, shown when that is at least 0.5. "It's a giveaway" is added only when the caption mentions a giveaway, entering or winners.
5. **Fastest growth:** the largest 7-day follower change in percent. This appears once 7 days of history exist.
6. **Collab:** the partner with the most co-authored posts.
7. **Offer-heavy account:** a `promo_share` of at least 50%.

When "you" is set, the dashboard pins your row and marks it "You".
