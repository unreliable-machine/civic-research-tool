"""
title: Civic Research Intelligence
author: ChangeAgent AI
description: Campaign finance, lobbying, influence networks, pay-to-play detection, and IRS 990 nonprofit filings — political money intelligence across FEC, Senate LDA, LittleSis, and IRS data.
version: 0.1.0
requirements: httpx
"""

import asyncio
import os
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

SYSTEM_PROMPT_INJECTION = """
### Civic Research Intelligence Tool

**This tool provides campaign finance, lobbying, political influence, and IRS nonprofit data.**

For targeted queries:
- **Who funds a politician?** → `crosswalk_legislator` to get their bioguide ID, then `legislator_funding_profile`
- **Who's lobbying on an issue?** → `search_lobbying`
- **What's an org's political influence?** → `org_influence_map`
- **Is there a pay-to-play pattern?** → `pay_to_play_analysis`
- **Who's connected to whom?** → `search_influence_network`, then `get_entity_network` for details
- **Campaign donations / PAC spending** → `search_campaign_finance`
- **Super PAC independent expenditures** → `search_expenditures`
- **Nonprofit IRS filings** → `search_irs_organizations` to find them, `search_irs_filings` for 990 data
- **Broad political money overview** → `generate_briefing` (combines lobbying, influence, and campaign finance)

**Use the other civic tools for:** legislation/bills (civic_legislators), grants/foundations (civic_funding), federal contracts (civic_procurement), court records (civic_court), nonprofits by sector (civic_organizations).

**Data coverage:** 437K influence entities, 1.8M relationships, 1.4M contribution aggregates, federal lobbying filings, FEC candidates/committees, 2.9M IRS 990 filings. Federal data only — state campaign finance not yet included.
"""


class EventEmitter:
    def __init__(self, event_emitter: Callable[[dict], Any] = None):
        self.event_emitter = event_emitter

    async def progress_update(self, description: str):
        await self.emit(description)

    async def error_update(self, description: str):
        await self.emit(description, "error", True)

    async def success_update(self, description: str):
        await self.emit(description, "success", True)

    async def emit(self, description="Unknown State", status="in_progress", done=False):
        if self.event_emitter:
            await self.event_emitter(
                {"type": "status", "data": {"status": status, "description": description, "done": done}}
            )


class Tools:
    class Valves(BaseModel):
        CIVIC_FINANCE_URL: str = Field(
            default_factory=lambda: os.getenv(
                "CIVIC_FINANCE_URL",
                "https://civic-finance-production.up.railway.app",
            ),
            description="Civic Finance API base URL (campaign finance, lobbying, influence)",
        )
        CIVIC_IRS_URL: str = Field(
            default_factory=lambda: os.getenv(
                "CIVIC_IRS_URL",
                "https://civic-irs-production.up.railway.app",
            ),
            description="Civic IRS API base URL (990 filings, exempt organizations)",
        )
        API_KEY: str = Field(
            default_factory=lambda: os.getenv("GOVCON_API_KEY", ""),
            description="Bearer token for API authentication (shared across services)",
        )
        TIMEOUT: int = Field(default=30, description="HTTP request timeout in seconds")
        COMPOSE_TIMEOUT: int = Field(default=60, description="Timeout for compose endpoints (slower, multi-source)")

    def __init__(self):
        self.valves = self.Valves()

    # ── HTTP helpers ──────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.valves.API_KEY:
            h["Authorization"] = f"Bearer {self.valves.API_KEY}"
        return h

    @staticmethod
    def _fmt_money(val) -> str:
        if val is None:
            return "N/A"
        try:
            return f"${float(val):,.0f}"
        except (ValueError, TypeError):
            return str(val)

    # ── Source URL builders ────────────────────────────────────────

    @staticmethod
    def _fec_candidate_url(candidate_id: str) -> str:
        return f"https://www.fec.gov/data/candidate/{candidate_id}/" if candidate_id else ""

    @staticmethod
    def _fec_committee_url(committee_id: str) -> str:
        return f"https://www.fec.gov/data/committee/{committee_id}/" if committee_id else ""

    @staticmethod
    def _lda_filing_url(filing_uuid: str) -> str:
        return f"https://lda.senate.gov/filings/public/filing/{filing_uuid}/" if filing_uuid else ""

    @staticmethod
    def _littlesis_url(entity_id) -> str:
        return f"https://littlesis.org/entities/{entity_id}" if entity_id else ""

    @staticmethod
    def _bioguide_url(bioguide_id: str) -> str:
        return f"https://bioguide.congress.gov/search/bio/{bioguide_id}" if bioguide_id else ""

    @staticmethod
    def _opensecrets_url(opensecrets_id: str) -> str:
        return f"https://www.opensecrets.org/members-of-congress/summary?cid={opensecrets_id}" if opensecrets_id else ""

    @staticmethod
    def _propublica_ein_url(ein: str) -> str:
        clean = str(ein).replace("-", "") if ein else ""
        return f"https://projects.propublica.org/nonprofits/organizations/{clean}" if clean else ""

    @staticmethod
    def _source_link(url: str, label: str) -> str:
        """Format a markdown source link, or empty string if no URL."""
        return f"[{label}]({url})" if url else ""

    def _sources_footer(self, sources: list) -> str:
        """Build a Sources footer from a list of (label, url) tuples. Deduplicates."""
        seen = set()
        unique = []
        for label, url in sources:
            if url and url not in seen:
                seen.add(url)
                unique.append(f"[{label}]({url})")
        if not unique:
            return ""
        return "\n\n---\n**Sources:** " + " | ".join(unique)

    async def _get(
        self,
        base_url: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> tuple:
        """Anti-fragile GET: 2 retries on 5xx/connection errors, graceful degradation.

        Returns (data_dict, None) on success or (None, error_string) on failure.
        Never raises.
        """
        import httpx

        url = f"{base_url.rstrip('/')}{path}"
        cleaned = {k: v for k, v in (params or {}).items() if v is not None}
        t = timeout or self.valves.TIMEOUT
        backoffs = [1, 3]

        last_error = ""
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=t) as client:
                    resp = await client.get(url, params=cleaned, headers=self._headers())
                    if resp.status_code >= 500 and attempt < 1:
                        last_error = f"Server error ({resp.status_code})"
                        await asyncio.sleep(backoffs[attempt])
                        continue
                    if resp.status_code == 401:
                        return None, "Authentication failed — check API key configuration"
                    if resp.status_code == 404:
                        return None, "Resource not found"
                    if resp.status_code >= 400:
                        return None, f"Request error ({resp.status_code})"
                    return resp.json(), None
            except httpx.TimeoutException:
                last_error = f"Request timed out after {t}s"
                if attempt < 1:
                    await asyncio.sleep(backoffs[attempt])
                    continue
            except httpx.ConnectError:
                last_error = "Service unavailable — connection failed"
                if attempt < 1:
                    await asyncio.sleep(backoffs[attempt])
                    continue
            except Exception as e:
                return None, f"Unexpected error: {str(e)[:200]}"

        return None, last_error

    # LittleSis relationship category labels
    _REL_CATEGORIES = {
        1: "Position", 2: "Education", 3: "Membership", 4: "Family",
        5: "Donation", 6: "Transaction", 7: "Lobbying", 8: "Social",
        9: "Professional", 10: "Ownership", 11: "Hierarchy", 12: "Generic",
    }

    def _fmt_relationship(self, rel: dict, context_entity_id=None) -> str:
        """Format a LittleSis relationship for display.
        Names are often null — show description, category, amount, and the other entity's ID."""
        e1_name = rel.get("entity1_name") or ""
        e2_name = rel.get("entity2_name") or ""
        desc = rel.get("description1") or rel.get("description2") or ""
        cat_id = rel.get("category_id")
        cat_label = self._REL_CATEGORIES.get(cat_id, "")
        amount = rel.get("amount")

        # Try to show the "other" entity name; fall back to ID
        if context_entity_id is not None:
            if str(rel.get("entity1_id", "")) == str(context_entity_id):
                other = e2_name or f"Entity #{rel.get('entity2_id', '?')}"
            else:
                other = e1_name or f"Entity #{rel.get('entity1_id', '?')}"
        else:
            other = e1_name or e2_name or "Unknown"

        label = desc or cat_label or "connected"
        entry = f"**{other}** — {label}"
        if amount:
            entry += f" ({self._fmt_money(amount)})"
        return entry

    def _finance_url(self) -> str:
        return self.valves.CIVIC_FINANCE_URL

    def _irs_url(self) -> str:
        return self.valves.CIVIC_IRS_URL

    # ── Search methods (fast, targeted) ───────────────────────────

    async def search_campaign_finance(
        self,
        query: str,
        data_type: str = "candidates",
        state: Optional[str] = None,
        party: Optional[str] = None,
        cycle: Optional[int] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search FEC campaign finance data — candidates, committees, or contribution aggregates.
        Use this for questions about who funds whom, PAC spending, political donations, and
        campaign fundraising.

        :param query: Search text (e.g., "sanders", "ActBlue", "tech PAC")
        :param data_type: What to search — "candidates" (default), "committees", or "contributions"
        :param state: Two-letter state code filter (e.g., "VT", "CA")
        :param party: Party filter (e.g., "DEM", "REP")
        :param cycle: Election cycle year (e.g., 2024)
        :param page: Page number (default: 1)
        :return: Campaign finance records with financial details and drill-down hints.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Searching FEC {data_type}: {query}")

        if data_type == "candidates":
            data, error = await self._get(self._finance_url(), "/api/v1/candidates", {
                "q": query, "state": state, "party": party, "cycle": cycle,
                "page": page, "page_size": 25,
            })
        elif data_type == "committees":
            data, error = await self._get(self._finance_url(), "/api/v1/committees", {
                "q": query, "state": state, "party": party,
                "page": page, "page_size": 25,
            })
        elif data_type == "contributions":
            data, error = await self._get(self._finance_url(), "/api/v1/contributions/aggregates", {
                "committee_id": query, "cycle": cycle,
                "page": page, "page_size": 25,
            })
        else:
            await emitter.error_update(f"Unknown data_type: {data_type}")
            return f"Invalid data_type '{data_type}'. Use 'candidates', 'committees', or 'contributions'."

        if error:
            await emitter.error_update(error)
            return f"⚠️ Campaign finance data unavailable: {error}. Try again in a moment."

        items = data.get("results", data.get("items", []))
        total = data.get("total_results", data.get("total", len(items)))

        if not items:
            await emitter.success_update("Search complete")
            return f"No {data_type} found for '{query}'. Try broadening your search or a different data_type."

        lines = [f"## FEC {data_type.title()}\n\nFound **{total}** results for \"{query}\"\n"]
        sources = []

        for i, item in enumerate(items[:10], 1):
            if data_type == "candidates":
                name = item.get("name", "Unknown")
                cand_id = item.get("candidate_id", "")
                cand_party = item.get("party", "")
                cand_state = item.get("state", "")
                office = item.get("office_full", item.get("office", ""))
                receipts = item.get("total_receipts")
                fec_link = self._fec_candidate_url(cand_id)
                name_display = f"[{name}]({fec_link})" if fec_link else f"**{name}**"
                lines.append(f"{i}. **{name_display}** ({cand_party}) — {cand_state}")
                parts = []
                if office:
                    parts.append(f"Office: {office}")
                if receipts is not None:
                    parts.append(f"Total receipts: {self._fmt_money(receipts)}")
                if cand_id:
                    parts.append(f"FEC ID: {cand_id}")
                if parts:
                    lines.append(f"   {' | '.join(parts)}")
                if fec_link:
                    sources.append(("FEC.gov", fec_link))
                lines.append("")

            elif data_type == "committees":
                name = item.get("name", "Unknown")
                cmt_id = item.get("committee_id", "")
                cmt_type = item.get("committee_type_full", item.get("committee_type", ""))
                connected = item.get("connected_org_name", "")
                fec_link = self._fec_committee_url(cmt_id)
                name_display = f"[{name}]({fec_link})" if fec_link else f"**{name}**"
                lines.append(f"{i}. **{name_display}**")
                parts = []
                if cmt_type:
                    parts.append(f"Type: {cmt_type}")
                if connected:
                    parts.append(f"Connected org: {connected}")
                if cmt_id:
                    parts.append(f"ID: {cmt_id}")
                if parts:
                    lines.append(f"   {' | '.join(parts)}")
                if fec_link:
                    sources.append(("FEC.gov", fec_link))
                lines.append("")

            else:  # contributions
                committee = item.get("committee_name", item.get("committee_id", "Unknown"))
                cmt_id = item.get("committee_id", "")
                total_amount = item.get("total", item.get("amount"))
                dimension = item.get("dimension", "")
                fec_link = self._fec_committee_url(cmt_id)
                lines.append(f"{i}. **{committee}**")
                parts = []
                if total_amount is not None:
                    parts.append(f"Amount: {self._fmt_money(total_amount)}")
                if dimension:
                    parts.append(f"Dimension: {dimension}")
                if fec_link:
                    parts.append(f"[FEC]({fec_link})")
                if parts:
                    lines.append(f"   {' | '.join(parts)}")
                lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        hint = {
            "candidates": "Use `crosswalk_legislator(name)` to get all IDs, then `legislator_funding_profile(bioguide_id)` for the full money profile.",
            "committees": "Use `search_campaign_finance(committee_id, data_type='contributions')` to see contribution aggregates.",
            "contributions": "Use `search_expenditures(query)` to see independent expenditures for/against candidates.",
        }
        lines.append(f"\n_Tip: {hint.get(data_type, '')}_")
        sources.append(("FEC Campaign Finance Data", "https://www.fec.gov/data/"))
        lines.append(self._sources_footer(sources))

        await emitter.success_update(f"Found {total} {data_type}")
        return "\n".join(lines)

    async def search_lobbying(
        self,
        query: str,
        search_type: str = "filings",
        filing_year: Optional[int] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search federal lobbying data from Senate LDA filings. Find who lobbies whom about
        what, lobbying spend by issue, and lobbyist-to-Congress contribution disclosures.

        :param query: Search text (e.g., "homelessness", "Vectis DC", "defense appropriations")
        :param search_type: "filings" (lobbying registrations, default) or "contributions" (lobbyist political contributions)
        :param filing_year: Filter by filing year (e.g., 2024)
        :param page: Page number (default: 1)
        :return: Lobbying filings or contributions with registrant, client, amounts, and issues.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Searching lobbying {search_type}: {query}")

        if search_type == "filings":
            data, error = await self._get(self._finance_url(), "/api/v1/lobbying/filings", {
                "q": query, "filing_year": filing_year,
                "page": page, "page_size": 25,
            })
        elif search_type == "contributions":
            data, error = await self._get(self._finance_url(), "/api/v1/lobbying/contributions", {
                "q": query, "page": page, "page_size": 25,
            })
        else:
            await emitter.error_update(f"Unknown search_type: {search_type}")
            return f"Invalid search_type '{search_type}'. Use 'filings' or 'contributions'."

        if error:
            await emitter.error_update(error)
            return f"⚠️ Lobbying data unavailable: {error}. Try again in a moment."

        items = data.get("results", data.get("items", []))
        total = data.get("total_results", data.get("total", len(items)))

        if not items:
            await emitter.success_update("Search complete")
            return f"No lobbying {search_type} found for '{query}'. Try broadening your search."

        lines = [f"## Lobbying {search_type.title()}\n\nFound **{total}** results for \"{query}\"\n"]
        sources = []

        for i, item in enumerate(items[:10], 1):
            if search_type == "filings":
                registrant = item.get("registrant_name", "Unknown")
                client = item.get("client_name", "")
                amount = item.get("income", item.get("expenses"))
                year = item.get("filing_year", "")
                filing_type = item.get("filing_type", "")
                filing_uuid = item.get("filing_uuid", "")
                lda_link = self._lda_filing_url(filing_uuid)

                header = f"{i}. **{registrant}**" + (f" → {client}" if client else "")
                lines.append(header)
                parts = []
                if amount is not None:
                    parts.append(f"Amount: {self._fmt_money(amount)}")
                if year:
                    parts.append(f"Year: {year}")
                if filing_type:
                    parts.append(f"Type: {filing_type}")
                if lda_link:
                    parts.append(f"[Filing]({lda_link})")
                    sources.append(("Senate LDA", lda_link))
                if parts:
                    lines.append(f"   {' | '.join(parts)}")
                # Extract issue descriptions from lobbying_activities
                activities = item.get("lobbying_activities", [])
                if isinstance(activities, list) and activities:
                    descs = [a.get("description", "") for a in activities if a.get("description")]
                    if descs:
                        lines.append(f"   Issues: {descs[0][:200]}")
                elif isinstance(activities, str):
                    lines.append(f"   Issues: {activities[:200]}")
                lines.append("")

            else:  # contributions
                payee = item.get("payee_name", item.get("payee", "Unknown"))
                contributor = item.get("contributor_name", item.get("contributor", ""))
                amount = item.get("amount")
                date = (item.get("date", item.get("contribution_date", "")) or "")[:10]
                filing_uuid = item.get("filing_uuid", "")
                lda_link = self._lda_filing_url(filing_uuid)

                lines.append(f"{i}. **{contributor or 'Unknown'}** → {payee}")
                parts = []
                if amount is not None:
                    parts.append(f"Amount: {self._fmt_money(amount)}")
                if date:
                    parts.append(f"Date: {date}")
                if lda_link:
                    parts.append(f"[Filing]({lda_link})")
                    sources.append(("Senate LDA", lda_link))
                if parts:
                    lines.append(f"   {' | '.join(parts)}")
                lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        lines.append(f"\n_Tip: Use `org_influence_map(org_name)` to see an org's full political footprint including lobbying._")
        sources.append(("Senate Lobbying Disclosure Act", "https://lda.senate.gov/filings/public/filing/search/"))
        lines.append(self._sources_footer(sources))

        await emitter.success_update(f"Found {total} lobbying {search_type}")
        return "\n".join(lines)

    async def search_influence_network(
        self,
        query: str,
        entity_type: Optional[str] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search the LittleSis power network — 437K entities and 1.8M relationships mapping
        who's connected to whom in politics, business, and government. Use this to find
        people and organizations and their relationship counts.

        :param query: Search text (e.g., "Koch", "Goldman Sachs", "Pelosi")
        :param entity_type: Filter by entity type (e.g., "Person", "Org")
        :param page: Page number (default: 1)
        :return: Influence entities with type, description, and relationship count.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Searching influence network: {query}")

        data, error = await self._get(self._finance_url(), "/api/v1/influence/entities", {
            "q": query, "entity_type": entity_type,
            "page": page, "page_size": 25,
        })

        if error:
            await emitter.error_update(error)
            return f"⚠️ Influence network unavailable: {error}. Try again in a moment."

        items = data.get("results", data.get("items", []))
        total = data.get("total_results", data.get("total", len(items)))

        if not items:
            await emitter.success_update("Search complete")
            return f"No influence entities found for '{query}'. Try a different spelling or broader search."

        lines = [f"## Influence Network Entities\n\nFound **{total}** results for \"{query}\"\n"]
        sources = []

        for i, item in enumerate(items[:10], 1):
            name = item.get("name", "Unknown")
            ent_type = item.get("primary_ext", item.get("entity_type", ""))
            ent_id = item.get("littlesis_id", item.get("id", ""))
            blurb = item.get("blurb", item.get("description", ""))
            rel_count = item.get("relationship_count", item.get("link_count", ""))
            ls_url = item.get("littlesis_url") or self._littlesis_url(ent_id)

            name_display = f"[{name}]({ls_url})" if ls_url else name
            lines.append(f"{i}. **{name_display}** ({ent_type})")
            parts = []
            if rel_count:
                parts.append(f"{rel_count} relationships")
            if ent_id:
                parts.append(f"ID: {ent_id}")
            if parts:
                lines.append(f"   {' | '.join(parts)}")
            if blurb:
                lines.append(f"   {str(blurb)[:200]}")
            if ls_url:
                sources.append(("LittleSis", ls_url))
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        lines.append(f"\n_Tip: Use `get_entity_network(entity_id)` to see an entity's full relationship map._")
        sources.append(("LittleSis Power Network", "https://littlesis.org"))
        lines.append(self._sources_footer(sources))

        await emitter.success_update(f"Found {total} influence entities")
        return "\n".join(lines)

    async def get_entity_network(
        self,
        entity_id: int,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Get the full relationship map for a specific LittleSis entity — shows all connections
        including board memberships, donations, lobbying ties, family relationships, and more.
        Use after search_influence_network to drill into a specific person or organization.

        :param entity_id: LittleSis entity ID (from search_influence_network results)
        :param page: Page number for relationships (default: 1)
        :return: Entity details and list of relationships with type, direction, and connected entities.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Fetching entity network for ID {entity_id}...")

        # Fetch entity detail and network in parallel
        entity_task = self._get(self._finance_url(), f"/api/v1/influence/entities/{entity_id}")
        network_task = self._get(
            self._finance_url(),
            f"/api/v1/influence/entities/{entity_id}/network",
            {"page": page, "page_size": 25},
        )
        (entity_data, entity_err), (network_data, network_err) = await asyncio.gather(
            entity_task, network_task
        )

        if entity_err and network_err:
            await emitter.error_update(entity_err)
            return f"⚠️ Entity network unavailable: {entity_err}. Try again in a moment."

        lines = []

        # Entity header
        if entity_data:
            name = entity_data.get("name", "Unknown")
            ent_type = entity_data.get("primary_ext", "")
            blurb = entity_data.get("blurb", "")
            lines.append(f"## {name}" + (f" ({ent_type})" if ent_type else ""))
            if blurb:
                lines.append(f"\n{blurb}\n")

        # Relationships
        if network_data:
            rels = network_data.get("results", network_data.get("relationships", []))
            total = network_data.get("total_results", network_data.get("total", len(rels)))

            if rels:
                lines.append(f"\n### Relationships ({total} total)\n")
                for i, rel in enumerate(rels[:15], 1):
                    lines.append(f"{i}. {self._fmt_relationship(rel, context_entity_id=entity_id)}")
                    lines.append("")

                if total > page * 25:
                    lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")
            else:
                lines.append("\nNo relationships found for this entity.")
        elif not entity_data:
            lines.append(f"No entity found with ID {entity_id}.")

        await emitter.success_update(f"Entity network retrieved")
        return "\n".join(lines)

    async def crosswalk_legislator(
        self,
        query: str,
        state: Optional[str] = None,
        chamber: Optional[str] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Look up a legislator's cross-referenced IDs — maps between bioguide, FEC, Open States,
        and OpenSecrets identifiers. Essential first step before calling legislator_funding_profile.
        Use this when you need to find a legislator's bioguide_id or other identifiers.

        :param query: Legislator name (e.g., "Pelosi", "Sanders", "Schumer")
        :param state: Two-letter state code filter (e.g., "CA", "VT")
        :param chamber: Chamber filter — "sen" (Senate) or "rep" (House)
        :param page: Page number (default: 1)
        :return: Legislator identifiers (bioguide, FEC IDs, Open States, OpenSecrets) with name and state.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Looking up legislator: {query}")

        data, error = await self._get(self._finance_url(), "/api/v1/crosswalk", {
            "q": query, "state": state, "chamber": chamber,
            "page": page, "page_size": 25,
        })

        if error:
            await emitter.error_update(error)
            return f"⚠️ Legislator crosswalk unavailable: {error}. Try again in a moment."

        items = data.get("results", data.get("items", []))
        total = data.get("total_results", data.get("total", len(items)))

        if not items:
            await emitter.success_update("Search complete")
            return f"No legislators found for '{query}'. Try a different spelling or just the last name."

        lines = [f"## Legislator ID Crosswalk\n\nFound **{total}** results for \"{query}\"\n"]

        for i, item in enumerate(items[:10], 1):
            name = item.get("name", "Unknown")
            bioguide = item.get("bioguide_id", "")
            leg_state = item.get("state", "")
            party = item.get("party", "")
            chamber_val = item.get("chamber", "")
            fec_ids = item.get("fec_ids", [])
            opensecrets = item.get("opensecrets_id", "")
            openstates = item.get("openstates_id", "")
            in_office = item.get("in_office")

            status = "In office" if in_office else ("Former" if in_office is not None else "")

            lines.append(f"{i}. **{name}** ({party}, {leg_state})")
            parts = []
            if chamber_val:
                parts.append(f"Chamber: {chamber_val}")
            if status:
                parts.append(status)
            if parts:
                lines.append(f"   {' | '.join(parts)}")

            id_parts = []
            if bioguide:
                bg_url = self._bioguide_url(bioguide)
                id_parts.append(f"Bioguide: [{bioguide}]({bg_url})")
            if fec_ids:
                fec_links = [f"[{f}]({self._fec_candidate_url(f)})" for f in fec_ids[:3]]
                id_parts.append(f"FEC: {', '.join(fec_links)}")
            if opensecrets:
                os_url = self._opensecrets_url(opensecrets)
                id_parts.append(f"OpenSecrets: [{opensecrets}]({os_url})")
            if openstates:
                id_parts.append(f"OpenStates: `{openstates}`")
            if id_parts:
                lines.append(f"   IDs: {' | '.join(id_parts)}")

            if bioguide:
                lines.append(f"   _→ `legislator_funding_profile(\"{bioguide}\")` for full money profile_")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        lines.append(self._sources_footer([
            ("Congress Bioguide", "https://bioguide.congress.gov"),
            ("FEC.gov", "https://www.fec.gov/data/"),
            ("OpenSecrets", "https://www.opensecrets.org"),
        ]))

        await emitter.success_update(f"Found {total} legislators")
        return "\n".join(lines)

    # ── Compose methods (slower, multi-source) ────────────────────

    async def legislator_funding_profile(
        self,
        bioguide_id: str,
        identifier_type: str = "bioguide",
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Get a complete money profile for a legislator — cross-references FEC campaign finance,
        linked committees, independent expenditures for/against, lobbying filings mentioning
        them, and LittleSis influence relationships. The most comprehensive view of who funds
        a politician and their financial connections.

        :param bioguide_id: The legislator's identifier (bioguide ID by default, e.g., "S000033" for Bernie Sanders)
        :param identifier_type: Type of identifier — "bioguide" (default), "openstates", or "fec"
        :return: Full funding intelligence report with identity, FEC data, committees, expenditures, lobbying, and influence ties.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Building funding profile for {bioguide_id}...")

        data, error = await self._get(
            self._finance_url(),
            f"/api/v1/compose/legislator-funding/{bioguide_id}",
            {"identifier_type": identifier_type},
            timeout=self.valves.COMPOSE_TIMEOUT,
        )

        if error:
            await emitter.error_update(error)
            return f"⚠️ Legislator funding profile unavailable: {error}. Verify the bioguide ID with `crosswalk_legislator(name)` first."

        lines = []

        # Identity section
        identity = data.get("identity", {})
        name = identity.get("name", "Unknown Legislator")
        party = identity.get("party", "")
        state = identity.get("state", "")
        lines.append(f"## Funding Profile: {name}")
        if party or state:
            lines.append(f"**{party}, {state}**\n")

        # FEC candidate data
        candidate = data.get("candidate")
        if candidate:
            lines.append("### FEC Campaign Finance")
            receipts = candidate.get("total_receipts")
            disbursements = candidate.get("total_disbursements")
            cash = candidate.get("cash_on_hand")
            if receipts is not None:
                lines.append(f"- Total receipts: {self._fmt_money(receipts)}")
            if disbursements is not None:
                lines.append(f"- Total disbursements: {self._fmt_money(disbursements)}")
            if cash is not None:
                lines.append(f"- Cash on hand: {self._fmt_money(cash)}")
            lines.append("")

        # Committees
        committees = data.get("committees", [])
        if committees:
            lines.append(f"### Linked Committees ({len(committees)})")
            for c in committees[:10]:
                c_name = c.get("name", "Unknown")
                c_type = c.get("committee_type_full", c.get("committee_type", ""))
                designation = c.get("designation_full", c.get("designation", ""))
                lines.append(f"- **{c_name}** ({c_type or designation})")
            if len(committees) > 10:
                lines.append(f"_...and {len(committees) - 10} more committees_")
            lines.append("")

        # Expenditures for/against
        exp_for = data.get("expenditures_for", [])
        exp_against = data.get("expenditures_against", [])
        if exp_for or exp_against:
            lines.append(f"### Independent Expenditures")
            if exp_for:
                lines.append(f"\n**Supporting ({len(exp_for)}):**")
                for e in exp_for[:5]:
                    spender = e.get("committee_name", e.get("committee_id", "Unknown"))
                    amount = e.get("expenditure_amount", e.get("amount"))
                    lines.append(f"- {spender}: {self._fmt_money(amount)}")
            if exp_against:
                lines.append(f"\n**Opposing ({len(exp_against)}):**")
                for e in exp_against[:5]:
                    spender = e.get("committee_name", e.get("committee_id", "Unknown"))
                    amount = e.get("expenditure_amount", e.get("amount"))
                    lines.append(f"- {spender}: {self._fmt_money(amount)}")
            lines.append("")

        # Lobbying
        lobbying = data.get("lobbying_filings", [])
        if lobbying:
            lines.append(f"### Lobbying Connections ({len(lobbying)})")
            for l in lobbying[:5]:
                reg = l.get("registrant_name", "Unknown")
                client = l.get("client_name", "")
                amount = l.get("income", l.get("expenses"))
                entry = f"- **{reg}**" + (f" (client: {client})" if client else "")
                if amount is not None:
                    entry += f" — {self._fmt_money(amount)}"
                lines.append(entry)
            if len(lobbying) > 5:
                lines.append(f"_...and {len(lobbying) - 5} more lobbying filings_")
            lines.append("")

        # Influence relationships
        influence = data.get("influence_relationships", [])
        if influence:
            lines.append(f"### Influence Network ({len(influence)} relationships)")
            for r in influence[:5]:
                lines.append(f"- {self._fmt_relationship(r)}")
            if len(influence) > 5:
                lines.append(f"_...and {len(influence) - 5} more relationships_")
            lines.append("")

        # Data freshness warnings
        warnings = data.get("stale_data_warnings", [])
        scope = data.get("data_scope", "")
        if warnings:
            lines.append("### ⚠️ Data Notes")
            for w in warnings:
                lines.append(f"- {w}")
        if scope:
            lines.append(f"\n_Scope: {scope}_")

        # Sources
        src = [("FEC Campaign Finance", "https://www.fec.gov/data/")]
        bioguide = identity.get("bioguide_id", "")
        if bioguide:
            src.append(("Congress Bioguide", self._bioguide_url(bioguide)))
        opensecrets = identity.get("opensecrets_id", "")
        if opensecrets:
            src.append(("OpenSecrets", self._opensecrets_url(opensecrets)))
        if influence:
            src.append(("LittleSis", "https://littlesis.org"))
        if lobbying:
            src.append(("Senate LDA", "https://lda.senate.gov/filings/public/filing/search/"))
        lines.append(self._sources_footer(src))

        await emitter.success_update(f"Funding profile complete for {name}")
        return "\n".join(lines)

    async def org_influence_map(
        self,
        org_name: str,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Map an organization's full political footprint — lobbying (as client and registrant),
        FEC committees/PACs, Super PAC spending, and LittleSis influence relationships.
        The comprehensive view of how an organization wields political influence.

        :param org_name: Organization name (e.g., "ExxonMobil", "National Alliance to End Homelessness", "Koch Industries")
        :return: Organization influence report with lobbying, committees, expenditures, and network relationships.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Mapping influence for {org_name}...")

        data, error = await self._get(
            self._finance_url(),
            "/api/v1/compose/org-influence",
            {"org_name": org_name},
            timeout=self.valves.COMPOSE_TIMEOUT,
        )

        if error:
            await emitter.error_update(error)
            return f"⚠️ Org influence data unavailable: {error}. Try again in a moment."

        lines = [f"## Influence Map: {org_name}\n"]

        # Lobbying as client
        client_filings = data.get("lobbying_as_client", [])
        if client_filings:
            lines.append(f"### Lobbying (as Client) — {len(client_filings)} filings")
            for f in client_filings[:5]:
                reg = f.get("registrant_name", "Unknown")
                amount = f.get("income", f.get("expenses"))
                year = f.get("filing_year", "")
                entry = f"- **{reg}**"
                parts = []
                if amount is not None:
                    parts.append(self._fmt_money(amount))
                if year:
                    parts.append(str(year))
                if parts:
                    entry += f" ({', '.join(parts)})"
                lines.append(entry)
            if len(client_filings) > 5:
                lines.append(f"_...and {len(client_filings) - 5} more_")
            lines.append("")

        # Lobbying as registrant
        reg_filings = data.get("lobbying_as_registrant", [])
        if reg_filings:
            lines.append(f"### Lobbying (as Registrant) — {len(reg_filings)} filings")
            for f in reg_filings[:5]:
                client = f.get("client_name", "Unknown")
                amount = f.get("income", f.get("expenses"))
                entry = f"- Client: **{client}**"
                if amount is not None:
                    entry += f" — {self._fmt_money(amount)}"
                lines.append(entry)
            if len(reg_filings) > 5:
                lines.append(f"_...and {len(reg_filings) - 5} more_")
            lines.append("")

        # Committees / PACs
        committees = data.get("committees", [])
        if committees:
            lines.append(f"### FEC Committees / PACs — {len(committees)}")
            for c in committees[:5]:
                c_name = c.get("name", "Unknown")
                c_type = c.get("committee_type_full", c.get("committee_type", ""))
                lines.append(f"- **{c_name}** ({c_type})")
            if len(committees) > 5:
                lines.append(f"_...and {len(committees) - 5} more_")
            lines.append("")

        # Independent expenditures
        expenditures = data.get("expenditures", [])
        if expenditures:
            lines.append(f"### Independent Expenditures — {len(expenditures)}")
            for e in expenditures[:5]:
                candidate = e.get("candidate_name", e.get("candidate_id", "Unknown"))
                amount = e.get("expenditure_amount", e.get("amount"))
                support = e.get("support_oppose_indicator", "")
                direction = "Supporting" if support == "S" else ("Opposing" if support == "O" else "")
                entry = f"- {direction} **{candidate}**: {self._fmt_money(amount)}"
                lines.append(entry)
            if len(expenditures) > 5:
                lines.append(f"_...and {len(expenditures) - 5} more_")
            lines.append("")

        # Influence entity + relationships
        entity = data.get("influence_entity")
        rels = data.get("influence_relationships", [])
        if entity or rels:
            lines.append(f"### LittleSis Influence Network")
            if entity:
                blurb = entity.get("blurb", "")
                if blurb:
                    lines.append(f"{blurb}\n")
            if rels:
                lines.append(f"**{len(rels)} relationships:**")
                for r in rels[:8]:
                    lines.append(f"- {self._fmt_relationship(r)}")
                if len(rels) > 8:
                    lines.append(f"_...and {len(rels) - 8} more relationships_")
            lines.append("")

        # Empty state
        if not any([client_filings, reg_filings, committees, expenditures, entity, rels]):
            lines.append(f"No political influence data found for '{org_name}'. Try an alternative name or parent company.")

        # Data freshness
        warnings = data.get("stale_data_warnings", [])
        scope = data.get("data_scope", "")
        if warnings:
            lines.append("### ⚠️ Data Notes")
            for w in warnings:
                lines.append(f"- {w}")
        if scope:
            lines.append(f"\n_Scope: {scope}_")

        # Sources
        src = []
        if client_filings or reg_filings:
            src.append(("Senate LDA Filings", "https://lda.senate.gov/filings/public/filing/search/"))
        if committees:
            src.append(("FEC Committees", "https://www.fec.gov/data/"))
        if entity:
            ent_id = entity.get("littlesis_id")
            src.append(("LittleSis", self._littlesis_url(ent_id) or "https://littlesis.org"))
        lines.append(self._sources_footer(src))

        await emitter.success_update(f"Influence map complete for {org_name}")
        return "\n".join(lines)

    async def pay_to_play_analysis(
        self,
        entity_name: str,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Cross-reference an entity's campaign contributions, lobbying filings, and government
        contracts to detect pay-to-play patterns. Shows overlap score indicating how many
        dimensions (contributions, lobbying, contracts) the entity appears in.

        :param entity_name: Entity name to investigate (e.g., "Lockheed Martin", "Raytheon", "Boeing")
        :return: Pay-to-play analysis with contributions, lobbying, contracts, and overlap score.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Running pay-to-play analysis for {entity_name}...")

        data, error = await self._get(
            self._finance_url(),
            "/api/v1/compose/pay-to-play",
            {"entity_name": entity_name},
            timeout=self.valves.COMPOSE_TIMEOUT,
        )

        if error:
            await emitter.error_update(error)
            return f"⚠️ Pay-to-play analysis unavailable: {error}. Try again in a moment."

        lines = [f"## Pay-to-Play Analysis: {entity_name}\n"]

        overlap = data.get("overlap_score", 0)
        if overlap >= 0.67:
            lines.append(f"**Overlap Score: {overlap:.0%}** — High overlap across contributions, lobbying, and contracts\n")
        elif overlap >= 0.33:
            lines.append(f"**Overlap Score: {overlap:.0%}** — Moderate overlap detected\n")
        else:
            lines.append(f"**Overlap Score: {overlap:.0%}** — Limited overlap\n")

        # Contributions
        contributions = data.get("contributions", [])
        if contributions:
            lines.append(f"### Campaign Contributions ({len(contributions)})")
            for c in contributions[:5]:
                committee = c.get("committee_name", c.get("committee_id", "Unknown"))
                total = c.get("total", c.get("amount"))
                cycle = c.get("cycle", "")
                entry = f"- **{committee}**"
                parts = []
                if total is not None:
                    parts.append(self._fmt_money(total))
                if cycle:
                    parts.append(f"Cycle: {cycle}")
                if parts:
                    entry += f" ({', '.join(parts)})"
                lines.append(entry)
            if len(contributions) > 5:
                lines.append(f"_...and {len(contributions) - 5} more_")
            lines.append("")

        # Lobbying
        lobbying = data.get("lobbying_filings", [])
        if lobbying:
            lines.append(f"### Lobbying Filings ({len(lobbying)})")
            for l in lobbying[:5]:
                reg = l.get("registrant_name", "Unknown")
                client = l.get("client_name", "")
                amount = l.get("income", l.get("expenses"))
                entry = f"- **{reg}**"
                if client:
                    entry += f" (client: {client})"
                if amount is not None:
                    entry += f" — {self._fmt_money(amount)}"
                lines.append(entry)
            if len(lobbying) > 5:
                lines.append(f"_...and {len(lobbying) - 5} more_")
            lines.append("")

        # Government contracts
        awards = data.get("awards", [])
        if awards:
            lines.append(f"### Government Contracts ({len(awards)})")
            for a in awards[:5]:
                recipient = a.get("recipient_name", "Unknown")
                amount = a.get("award_amount", a.get("total_obligation"))
                agency = a.get("agency_name", "")
                desc = a.get("description", "")
                entry = f"- **{recipient}**: {self._fmt_money(amount)}"
                if agency:
                    entry += f" ({agency})"
                lines.append(entry)
                if desc:
                    lines.append(f"  {str(desc)[:150]}")
            if len(awards) > 5:
                lines.append(f"_...and {len(awards) - 5} more_")
            lines.append("")

        # Empty state
        if not any([contributions, lobbying, awards]):
            lines.append(f"No pay-to-play data found for '{entity_name}'. Try the parent company name or a common alias.")

        # Data freshness
        warnings = data.get("stale_data_warnings", [])
        scope = data.get("data_scope", "")
        if warnings:
            lines.append("### ⚠️ Data Notes")
            for w in warnings:
                lines.append(f"- {w}")
        if scope:
            lines.append(f"\n_Scope: {scope}_")

        # Sources
        src = []
        if contributions:
            src.append(("FEC Campaign Finance", "https://www.fec.gov/data/"))
        if lobbying:
            src.append(("Senate LDA Filings", "https://lda.senate.gov/filings/public/filing/search/"))
        if awards:
            src.append(("USAspending.gov", "https://www.usaspending.gov"))
        lines.append(self._sources_footer(src))

        await emitter.success_update(f"Pay-to-play analysis complete for {entity_name}")
        return "\n".join(lines)

    async def search_expenditures(
        self,
        query: str,
        candidate_id: Optional[str] = None,
        support_oppose: Optional[str] = None,
        state: Optional[str] = None,
        cycle: Optional[int] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search FEC independent expenditures — Super PAC spending for or against candidates.
        These are expenditures made by committees (PACs, Super PACs) that are not coordinated
        with the candidate's campaign.

        :param query: Search text (e.g., committee name, candidate name)
        :param candidate_id: FEC candidate ID to filter expenditures for/against a specific candidate
        :param support_oppose: Filter by direction — "S" (support) or "O" (oppose)
        :param state: Two-letter state filter
        :param cycle: Election cycle year (e.g., 2024)
        :param page: Page number (default: 1)
        :return: Independent expenditures with committee, candidate, amount, and support/oppose indicator.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Searching independent expenditures: {query}")

        data, error = await self._get(self._finance_url(), "/api/v1/expenditures", {
            "q": query, "candidate_id": candidate_id,
            "support_oppose": support_oppose, "state": state, "cycle": cycle,
            "page": page, "page_size": 25,
        })

        if error:
            await emitter.error_update(error)
            return f"⚠️ Expenditure data unavailable: {error}. Try again in a moment."

        items = data.get("results", data.get("items", []))
        total = data.get("total_results", data.get("total", len(items)))

        if not items:
            await emitter.success_update("Search complete")
            return f"No independent expenditures found for '{query}'. Try a different search term."

        lines = [f"## Independent Expenditures\n\nFound **{total}** results for \"{query}\"\n"]

        for i, item in enumerate(items[:10], 1):
            committee = item.get("committee_name", item.get("committee_id", "Unknown"))
            candidate = item.get("candidate_name", item.get("candidate_id", ""))
            amount = item.get("expenditure_amount", item.get("amount"))
            so = item.get("support_oppose_indicator", "")
            direction = "SUPPORTING" if so == "S" else ("OPPOSING" if so == "O" else "")
            date = (item.get("expenditure_date", item.get("date", "")) or "")[:10]
            purpose = item.get("purpose", item.get("expenditure_description", ""))

            lines.append(f"{i}. **{committee}**")
            parts = []
            if direction and candidate:
                parts.append(f"{direction} {candidate}")
            if amount is not None:
                parts.append(self._fmt_money(amount))
            if date:
                parts.append(date)
            if parts:
                lines.append(f"   {' | '.join(parts)}")
            if purpose:
                lines.append(f"   Purpose: {str(purpose)[:150]}")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        lines.append(self._sources_footer([("FEC Independent Expenditures", "https://www.fec.gov/data/independent-expenditures/")]))

        await emitter.success_update(f"Found {total} independent expenditures")
        return "\n".join(lines)

    async def generate_briefing(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Generate a multi-source intelligence briefing on any political topic by combining
        lobbying, influence network, and campaign finance data. Use this for broad questions
        like "tell me about AI regulation lobbying" or "who's involved in housing policy."
        Pulls from multiple endpoints and synthesizes results.

        :param query: Topic to research (e.g., "AI regulation", "homelessness policy", "defense spending")
        :return: Intelligence briefing combining lobbying filings, influence entities, and campaign finance data.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Generating intelligence briefing: {query}")

        # Fan out to three data sources in parallel
        lobbying_task = self._get(self._finance_url(), "/api/v1/lobbying/filings", {
            "q": query, "page": 1, "page_size": 10,
        })
        influence_task = self._get(self._finance_url(), "/api/v1/influence/entities", {
            "q": query, "page": 1, "page_size": 10,
        })
        candidates_task = self._get(self._finance_url(), "/api/v1/candidates", {
            "q": query, "page": 1, "page_size": 10,
        })

        (lobby_data, lobby_err), (inf_data, inf_err), (cand_data, cand_err) = await asyncio.gather(
            lobbying_task, influence_task, candidates_task
        )

        lines = [f"## Intelligence Briefing: {query}\n"]
        sections_found = 0

        # Lobbying section
        if lobby_data and not lobby_err:
            items = lobby_data.get("results", lobby_data.get("items", []))
            total = lobby_data.get("total_results", lobby_data.get("total", len(items)))
            if items:
                sections_found += 1
                lines.append(f"### Lobbying Activity ({total} filings found)\n")
                for item in items[:5]:
                    reg = item.get("registrant_name", "Unknown")
                    client = item.get("client_name", "")
                    amount = item.get("income", item.get("expenses"))
                    year = item.get("filing_year", "")
                    entry = f"- **{reg}**"
                    if client:
                        entry += f" → {client}"
                    parts = []
                    if amount is not None:
                        parts.append(self._fmt_money(amount))
                    if year:
                        parts.append(str(year))
                    if parts:
                        entry += f" ({', '.join(parts)})"
                    lines.append(entry)
                if total > 5:
                    lines.append(f"_...{total - 5} more filings. Use `search_lobbying(\"{query}\")` for full results._")
                lines.append("")

        # Influence network section
        if inf_data and not inf_err:
            items = inf_data.get("results", inf_data.get("items", []))
            total = inf_data.get("total_results", inf_data.get("total", len(items)))
            if items:
                sections_found += 1
                lines.append(f"### Key Players ({total} entities found)\n")
                for item in items[:5]:
                    name = item.get("name", "Unknown")
                    ent_type = item.get("primary_ext", item.get("entity_type", ""))
                    rel_count = item.get("relationship_count", item.get("link_count", ""))
                    blurb = item.get("blurb", "")
                    entry = f"- **{name}** ({ent_type})"
                    if rel_count:
                        entry += f" — {rel_count} connections"
                    lines.append(entry)
                    if blurb:
                        lines.append(f"  {str(blurb)[:150]}")
                if total > 5:
                    lines.append(f"_...{total - 5} more entities. Use `search_influence_network(\"{query}\")` for full results._")
                lines.append("")

        # Campaign finance section
        if cand_data and not cand_err:
            items = cand_data.get("results", cand_data.get("items", []))
            total = cand_data.get("total_results", cand_data.get("total", len(items)))
            if items:
                sections_found += 1
                lines.append(f"### Related Candidates ({total} found)\n")
                for item in items[:5]:
                    name = item.get("name", "Unknown")
                    party = item.get("party", "")
                    state = item.get("state", "")
                    receipts = item.get("total_receipts")
                    entry = f"- **{name}** ({party}, {state})"
                    if receipts is not None:
                        entry += f" — {self._fmt_money(receipts)} raised"
                    lines.append(entry)
                if total > 5:
                    lines.append(f"_...{total - 5} more. Use `search_campaign_finance(\"{query}\")` for full results._")
                lines.append("")

        if sections_found == 0:
            all_errors = [e for e in [lobby_err, inf_err, cand_err] if e]
            if all_errors:
                await emitter.error_update("Data sources unavailable")
                return f"⚠️ Intelligence briefing unavailable — data sources returned errors: {'; '.join(all_errors)}"
            else:
                await emitter.success_update("Briefing complete")
                return f"No political intelligence data found for '{query}'. Try more specific terms like a person, organization, or policy area name."

        lines.append("---")
        lines.append(f"_To dig deeper: `search_lobbying(\"{query}\")`, `search_influence_network(\"{query}\")`, or `org_influence_map(\"org name\")` for a specific organization._")
        lines.append(f"_Data scope: Federal FEC filings only. State-level campaign finance and dark money (501(c)(4)) not included._")
        src = [("FEC.gov", "https://www.fec.gov/data/")]
        if lobby_data and not lobby_err:
            src.append(("Senate LDA", "https://lda.senate.gov/filings/public/filing/search/"))
        if inf_data and not inf_err:
            src.append(("LittleSis", "https://littlesis.org"))
        lines.append(self._sources_footer(src))

        await emitter.success_update(f"Briefing complete — {sections_found} data sources")
        return "\n".join(lines)

    # ── IRS methods (civic-irs service directly) ──────────────────

    async def search_irs_organizations(
        self,
        query: str,
        state: Optional[str] = None,
        subsection: Optional[str] = None,
        ntee: Optional[str] = None,
        is_foundation: Optional[bool] = None,
        min_assets: Optional[int] = None,
        sort: Optional[str] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search IRS-registered tax-exempt organizations from the Business Master File (2.9M orgs).
        Find nonprofits, charities, foundations, and other exempt organizations by name, state,
        NTEE classification, or asset size.

        :param query: Organization name search (e.g., "Red Cross", "habitat for humanity")
        :param state: Two-letter state code filter
        :param subsection: IRS subsection code (e.g., "03" for 501(c)(3), "04" for 501(c)(4))
        :param ntee: NTEE classification code filter (e.g., "P20" for human services)
        :param is_foundation: Filter for private foundations only (true/false)
        :param min_assets: Minimum asset value in USD
        :param sort: Sort field — "name", "assets", or "income"
        :param page: Page number (default: 1)
        :return: List of tax-exempt organizations with EIN, name, state, classification, and financial data.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Searching IRS exempt organizations: {query}")

        data, error = await self._get(self._irs_url(), "/api/organizations", {
            "q": query, "state": state, "subsection": subsection,
            "ntee": ntee, "is_foundation": is_foundation,
            "min_assets": min_assets, "sort": sort,
            "page": page, "page_size": 25,
        })

        if error:
            await emitter.error_update(error)
            return f"⚠️ IRS organization data unavailable: {error}. Try again in a moment."

        items = data.get("results", data.get("items", []))
        total = data.get("total_results", data.get("total", len(items)))

        if not items:
            await emitter.success_update("Search complete")
            return f"No IRS exempt organizations found for '{query}'. Try a different name or broader search."

        lines = [f"## IRS Exempt Organizations\n\nFound **{total}** results for \"{query}\"\n"]

        for i, item in enumerate(items[:10], 1):
            name = item.get("name", item.get("organization_name", "Unknown"))
            ein = item.get("ein", "")
            org_state = item.get("state", "")
            subsection_val = item.get("subsection", "")
            ntee_val = item.get("ntee_code", item.get("ntee", ""))
            assets = item.get("asset_amount", item.get("total_assets"))
            income = item.get("income_amount", item.get("total_income"))
            ruling_date = item.get("ruling_date", "")

            sub_label = f"501(c)({subsection_val})" if subsection_val else ""
            lines.append(f"{i}. **{name}** (EIN: {ein})")
            parts = []
            if org_state:
                parts.append(org_state)
            if sub_label:
                parts.append(sub_label)
            if ntee_val:
                parts.append(f"NTEE: {ntee_val}")
            if parts:
                lines.append(f"   {' | '.join(parts)}")

            money_parts = []
            if assets is not None:
                money_parts.append(f"Assets: {self._fmt_money(assets)}")
            if income is not None:
                money_parts.append(f"Income: {self._fmt_money(income)}")
            if money_parts:
                lines.append(f"   {' | '.join(money_parts)}")

            pp_url = self._propublica_ein_url(ein)
            if pp_url:
                lines.append(f"   _→ `search_irs_filings(\"{ein}\")` for 990s | [ProPublica]({pp_url})_")
            else:
                lines.append(f"   _→ `search_irs_filings(\"{ein}\")` for 990 filing history_")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        lines.append(self._sources_footer([
            ("IRS Business Master File", "https://www.irs.gov/charities-non-profits/tax-exempt-organization-search"),
            ("ProPublica Nonprofit Explorer", "https://projects.propublica.org/nonprofits/"),
        ]))

        await emitter.success_update(f"Found {total} organizations")
        return "\n".join(lines)

    async def search_irs_filings(
        self,
        ein: str,
        form_type: Optional[str] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Get all IRS 990 filings for a nonprofit by EIN — shows revenue, expenses, and assets
        over time. Use after search_irs_organizations to drill into a specific organization's
        filing history.

        :param ein: Employer Identification Number (e.g., "13-1837418" or "131837418")
        :param form_type: Filter by form type (e.g., "990", "990EZ", "990PF")
        :param page: Page number (default: 1)
        :return: Filing history with tax periods, form types, and financial summaries.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Fetching 990 filings for EIN {ein}...")

        data, error = await self._get(self._irs_url(), f"/api/filings/{ein}", {
            "form_type": form_type, "page": page, "page_size": 25,
        })

        if error:
            await emitter.error_update(error)
            return f"⚠️ IRS filing data unavailable for EIN {ein}: {error}. Verify the EIN with `search_irs_organizations(name)`."

        items = data.get("results", data.get("filings", data.get("items", [])))
        total = data.get("total_results", data.get("total", len(items)))
        org_name = data.get("organization_name", data.get("name", f"EIN {ein}"))

        if not items:
            await emitter.success_update("Search complete")
            return f"No 990 filings found for EIN {ein}. The organization may not have filed electronically, or try `search_irs_organizations` to verify the EIN."

        lines = [f"## IRS 990 Filings: {org_name}\n\nEIN: {ein} | **{total}** filings found\n"]

        # Table format for filing history
        lines.append("| Tax Period | Form | Revenue | Expenses | Assets |")
        lines.append("|-----------|------|---------|----------|--------|")

        for item in items[:15]:
            period = item.get("tax_period", item.get("tax_prd", ""))
            form = item.get("form_type", item.get("return_type", ""))
            revenue = item.get("total_revenue", item.get("totrevenue"))
            expenses = item.get("total_expenses", item.get("totfuncexpns"))
            assets = item.get("total_assets", item.get("totassetsend"))

            rev_str = self._fmt_money(revenue) if revenue is not None else "—"
            exp_str = self._fmt_money(expenses) if expenses is not None else "—"
            asset_str = self._fmt_money(assets) if assets is not None else "—"

            lines.append(f"| {period} | {form} | {rev_str} | {exp_str} | {asset_str} |")

        lines.append("")

        if total > 15:
            lines.append(f"_Showing first 15 of {total} filings. Use page={page + 1} for more._")

        lines.append(f"\n_Note: Financial data availability depends on electronic filing. Older filings may have limited data._")

        pp_url = self._propublica_ein_url(ein)
        lines.append(self._sources_footer([
            ("IRS 990 Electronic Filings", "https://www.irs.gov/charities-non-profits/tax-exempt-organization-search"),
            ("ProPublica Nonprofit Explorer", pp_url or "https://projects.propublica.org/nonprofits/"),
        ]))

        await emitter.success_update(f"Found {total} filings for {org_name}")
        return "\n".join(lines)
