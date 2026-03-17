# Civic Research Intelligence Tool

Open WebUI tool for campaign finance, lobbying, influence networks, pay-to-play detection, and IRS 990 filings.

Part of the [Civic Intelligence Platform](https://github.com/unreliable-machine/civic-tools) for [Change Agent AI](https://thechange.ai).

## Installation

1. Open your Open WebUI instance → **Admin Panel** → **Tools** → **+**
2. Paste the contents of `civic_research.py`
3. Save → configure Valves (gear icon)

## Valves

| Valve | Value |
|-------|-------|
| `CIVIC_FINANCE_URL` | `https://civic-finance-production.up.railway.app` |
| `CIVIC_IRS_URL` | `https://civic-irs-production.up.railway.app` |
| `API_KEY` | Your GOVCON API key |
| `TIMEOUT` | `30` |
| `COMPOSE_TIMEOUT` | `60` |

## Methods

- `search_campaign_finance`
- `search_lobbying`
- `search_influence_network`
- `get_entity_network`
- `crosswalk_legislator`
- `legislator_funding_profile`
- `org_influence_map`
- `pay_to_play_analysis`
- `search_expenditures`
- `generate_briefing`
- `search_irs_organizations`
- `search_irs_filings`

## Test

```
Any pay-to-play with Lockheed Martin?
```

## Backend API

`civic-finance + civic-irs`

## Related

- [civic-tools](https://github.com/unreliable-machine/civic-tools) — umbrella repo with all 7 civic tools
- [civic-finance](https://github.com/unreliable-machine/civic-finance) — campaign finance microservice
- [civic-irs](https://github.com/unreliable-machine/civic-irs) — IRS 990 filings microservice
- [govcon-intelligence](https://github.com/unreliable-machine/govcon-intelligence) — procurement, grants, legislators, courts

## License

MIT
