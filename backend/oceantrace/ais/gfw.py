"""Global Fishing Watch (GFW) API client for vessel identity lookup and enrichment."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..core.config import get_settings

log = logging.getLogger("oceantrace.ais.gfw")
GFW_GATEWAY_URL = "https://gateway.api.globalfishingwatch.org/v3"


def search_vessel_gfw(query: str | int, limit: int = 5) -> list[dict[str, Any]]:
    """Search vessel identity in Global Fishing Watch public dataset."""
    token = get_settings().gfw_api_token
    if not token:
        log.warning("OT_GFW_API_TOKEN is not configured; skipping GFW search.")
        return []

    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "query": str(query),
        "datasets[0]": "public-global-vessel-identity:latest",
        "limit": limit,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{GFW_GATEWAY_URL}/vessels/search", headers=headers, params=params)
            if resp.status_code != 200:
                log.error("GFW API search failed with status %s: %s", resp.status_code, resp.text)
                return []
            data = resp.json()
            entries = data.get("entries", [])
            results = []
            for entry in entries:
                # Extract registry info or self-reported info
                reg = (entry.get("registryInfo") or [{}])[0] if entry.get("registryInfo") else {}
                self_rep = (entry.get("selfReportedInfo") or [{}])[0] if entry.get("selfReportedInfo") else {}
                
                name = reg.get("shipname") or self_rep.get("shipname")
                flag = reg.get("flag") or self_rep.get("flag")
                callsign = reg.get("callsign") or self_rep.get("callsign")
                imo = reg.get("imo") or self_rep.get("imo")
                mmsi = reg.get("ssvid") or self_rep.get("ssvid")
                geartypes = reg.get("geartypes") or []
                shiptypes = [st.get("name") for st in entry.get("combinedSourcesInfo", [{}])[0].get("shiptypes", [])] if entry.get("combinedSourcesInfo") else []

                results.append({
                    "mmsi": mmsi,
                    "name": name,
                    "flag": flag,
                    "callsign": callsign,
                    "imo": imo,
                    "geartypes": geartypes,
                    "shiptypes": shiptypes,
                    "tonnage_gt": reg.get("tonnageGt"),
                    "length_m": reg.get("lengthM"),
                })
            return results
    except Exception as exc:
        log.exception("Error querying Global Fishing Watch: %s", exc)
        return []


def enrich_vessel_details(mmsi: int | str, current_name: str | None = None) -> dict[str, Any]:
    """Look up vessel details in GFW to enrich AIS records with official flag, IMO, callsign."""
    res = search_vessel_gfw(mmsi, limit=3)
    if not res:
        return {}
    # Prioritize entry matching MMSI
    for entry in res:
        if str(entry.get("mmsi")) == str(mmsi):
            return entry
    return res[0]
