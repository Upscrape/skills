# Upscrape Skills

Agent skills that do real work with live data from [Upscrape](https://upscrape.com). They follow the open SKILL.md format, so they work in Claude Code, Codex, Cursor and other agents that read it.

## instagram-competitor-tracker

Give your agent a few Instagram accounts. It tracks them day by day and hands back one dashboard file with five tabs (Overview, Accounts, Posts, Content, Growth):

- **What stands out:** breakout posts, engagement gaps, giveaways, offers and collabs, in plain sentences.
- **Side by side:** followers, 7-day change, posts a week, engagement rate, format mix, offer share and breakouts.
- **Every post, by day:** each account's last 28 days on one timeline, sized by how well a post did against that account's usual.
- **Follower change, what format works, and who they collab with.**

It is one HTML file with no dependencies. It works offline, has light and dark modes, and is easy to share.

### Install

```bash
npx skills add upscrape/skills
```

Or copy `skills/instagram-competitor-tracker` into your agent's skills folder.

### Use

Set `UPSCRAPE_API_KEY`, or connect the Upscrape MCP server (`https://data.upscrape.com/mcp`). Then ask your agent:

> Track @glossier, @rarebeauty and @fentybeauty on Instagram.

If one of the accounts is yours, say so and it gets marked "You".

Each account costs 5 credits a run. Failed calls are free. The agent shows you the cost before the first run.

### Run it daily and brand it

> Run this every day, and brand it for my client Acme Beauty with our agency's logo.

The agent sets up a daily GitHub Actions run, so history keeps building while your computer is off (use a private repo), and puts your agency's name, logo and colour on the report.

## License

MIT. The dashboard embeds the Manrope font under the SIL Open Font License.
