"""Serveur MCP personnel pour l'API Garmin Connect Personal (lecture seule).

- La clé API Garmin reste côté serveur (variable GARMIN_PERSONAL_API_KEY).
- Chaque requête MCP doit porter l'en-tête `Authorization: Bearer <MCP_AUTH_TOKEN>`.
- Le jeton d'accès Garmin (valable 24 h, sans refresh token) est renouvelé à la demande.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import time
from datetime import date, timedelta
from typing import Any

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

GARMIN_BASE_URL = os.environ.get(
    "GARMIN_PERSONAL_API_BASE_URL", "https://personal.connectapi.garmin.com"
).rstrip("/")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
GARMIN_API_KEY = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8000"))
MAX_LIST_ITEMS = int(os.environ.get("MAX_LIST_ITEMS", "40"))
REFRESH_WINDOW_SECONDS = 3600

if not GARMIN_API_KEY:
    raise SystemExit("GARMIN_PERSONAL_API_KEY est vide : renseigne-la dans le .env")
if len(MCP_AUTH_TOKEN) < 32:
    raise SystemExit("MCP_AUTH_TOKEN doit faire au moins 32 caractères (openssl rand -hex 32)")


# --------------------------------------------------------------------------- Garmin


class GarminClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(base_url=GARMIN_BASE_URL, timeout=30.0)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _bearer(self, force: bool = False) -> str:
        async with self._lock:
            fresh = self._token and self._expires_at - time.time() > REFRESH_WINDOW_SECONDS
            if fresh and not force:
                return self._token  # type: ignore[return-value]
            response = await self._http.post(
                TOKEN_PATH,
                headers={"Accept": "*/*", "Authorization": f"ApiKey {GARMIN_API_KEY}"},
            )
            if response.status_code >= 400:
                raise GarminError(
                    f"Garmin a refusé la clé API (HTTP {response.status_code}). "
                    "Clé expirée ou supprimée ? Régénère-la dans Garmin Connect."
                )
            self._token = response.text.strip()
            self._expires_at = _jwt_expiration(self._token)
            return self._token

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in (0, 1):
            token = await self._bearer(force=attempt == 1)
            response = await self._http.get(
                path,
                params=params,
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code >= 400:
                raise GarminError(
                    f"Garmin HTTP {response.status_code} sur {path} : {response.text[:300]}"
                )
            return response.json() if response.content else None
        return None


class GarminError(Exception):
    pass


def _jwt_expiration(token: str) -> float:
    """Lit la date d'expiration du JWT ; 23 h par défaut si elle est illisible."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return time.time() + 23 * 3600


def compact(value: Any, max_items: int) -> Any:
    """Retire les valeurs nulles et tronque les longues listes pour économiser des tokens."""
    if isinstance(value, dict):
        out = {k: compact(v, max_items) for k, v in value.items() if v is not None}
        return {k: v for k, v in out.items() if v not in ({}, [])}
    if isinstance(value, list):
        if max_items and len(value) > max_items:
            kept = [compact(v, max_items) for v in value[:max_items]]
            kept.append({"_tronque": f"{len(value) - max_items} éléments omis sur {len(value)}"})
            return kept
        return [compact(v, max_items) for v in value]
    return value


def _local_hhmm(ms: Any) -> str | None:
    """Garmin fournit des horodatages « locaux » en millisecondes : on n'en garde que HH:MM."""
    if not isinstance(ms, (int, float)):
        return None
    return time.strftime("%H:%M", time.gmtime(ms / 1000))


def _minutes(seconds: Any) -> int | None:
    return round(seconds / 60) if isinstance(seconds, (int, float)) else None


def slim_sleep(data: Any) -> Any:
    """Une ligne courte par nuit au lieu de la réponse Garmin complète."""
    if not isinstance(data, list):
        return data
    nights = []
    for n in data:
        if not isinstance(n, dict):
            continue
        overall = (n.get("sleepScores") or {}).get("overall") or {}
        nights.append(
            {
                "date": n.get("calendarDate"),
                "sommeil_min": _minutes(n.get("sleepTimeSeconds")),
                "profond_min": _minutes(n.get("deepSleepSeconds")),
                "leger_min": _minutes(n.get("lightSleepSeconds")),
                "paradoxal_min": _minutes(n.get("remSleepSeconds")),
                "eveil_min": _minutes(n.get("awakeSleepSeconds")),
                "coucher": _local_hhmm(n.get("sleepStartTimestampLocal")),
                "lever": _local_hhmm(n.get("sleepEndTimestampLocal")),
                "score": overall.get("value"),
                "qualite": overall.get("qualifierKey"),
                "fc_moy": n.get("avgHeartRate"),
                "stress_moy": n.get("avgSleepStress"),
                "reveils": n.get("awakeCount"),
            }
        )
    return nights


def slim_daily(data: Any) -> Any:
    """Une ligne courte par jour au lieu de la réponse Garmin complète."""
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return data
    days = []
    for d in rows:
        if not isinstance(d, dict):
            continue
        steps = (d.get("movement") or {}).get("steps") or {}
        kcal = d.get("calories") or {}
        hr = d.get("heartRate") or {}
        minutes = d.get("intensityMinutes") or {}
        battery = d.get("bodyBattery") or {}
        duration = (d.get("wellnessChronology") or {}).get("totalDurationInMillis")
        days.append(
            {
                "date": d.get("calendarDate"),
                "journee_partielle": isinstance(duration, (int, float)) and duration < 86_400_000,
                "pas": steps.get("value"),
                "objectif_pas": steps.get("goal"),
                "distance_m": steps.get("distanceInMeters"),
                "kcal_actives": kcal.get("burnedActive"),
                "kcal_totales": kcal.get("burnedTotal"),
                "fc_repos": hr.get("restingValue"),
                "fc_min": hr.get("minValue"),
                "fc_max": hr.get("maxValue"),
                "intensite_moderee_min": minutes.get("moderate"),
                "intensite_soutenue_min": minutes.get("vigorous"),
                "stress_moy": (d.get("stress") or {}).get("avgLevel"),
                "body_battery_min": battery.get("minValue"),
                "body_battery_max": battery.get("maxValue"),
            }
        )
    return days


def _dates(start_date: str | None, end_date: str | None, default_days: int) -> tuple[str, str]:
    end = date.fromisoformat(end_date) if end_date else date.today()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=default_days - 1)
    if start > end:
        raise GarminError("start_date doit précéder end_date")
    return start.isoformat(), end.isoformat()


garmin = GarminClient()


def _dump(value: Any) -> str:
    """JSON compact (sans indentation) pour limiter les tokens consommés."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _call(
    path: str,
    params: dict[str, Any] | None = None,
    full: bool = False,
    slim: Any = None,
) -> str:
    try:
        data = await garmin.get(path, params)
    except GarminError as exc:
        return _dump({"erreur": str(exc)})
    except (httpx.HTTPError, ValueError) as exc:
        return _dump({"erreur": f"Appel Garmin impossible : {exc}"})
    if data is None:
        return _dump({"resultat": "aucune donnée"})
    if full:
        return _dump(data)
    if slim is not None:
        data = slim(data)
    return _dump(compact(data, MAX_LIST_ITEMS))


# ------------------------------------------------------------------------------ MCP

mcp = MCPServer(
    "garmin-perso",
    instructions=(
        "Données Garmin Connect personnelles en lecture seule. Dates au format AAAA-MM-JJ. "
        "Préférer des fenêtres courtes (7 à 30 jours)."
    ),
)
READ_ONLY = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Poids", **READ_ONLY))
async def garmin_weight(start_date: str | None = None, end_date: str | None = None) -> str:
    """Pesées (balance connectée ou saisie manuelle) sur une période. Défaut : 30 derniers jours."""
    start, end = _dates(start_date, end_date, 30)
    return await _call(f"/weight-service/weight/range/{start}/{end}", {"includeAll": "true"})


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Activités", **READ_ONLY))
async def garmin_activities(
    start_date: str | None = None, end_date: str | None = None, limit: int = 20
) -> str:
    """Liste résumée des activités (sport, durée, distance, FC). Défaut : 14 derniers jours."""
    start, end = _dates(start_date, end_date, 14)
    return await _call(
        "/activitylist-service/activities/search/activities",
        {"start": 0, "limit": max(1, min(limit, 100)), "startDate": start, "endDate": end},
    )


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Détail d'une activité", **READ_ONLY))
async def garmin_activity(activity_id: int) -> str:
    """Détail d'une activité à partir de son identifiant (voir garmin_activities)."""
    return await _call(f"/activity-service/activity/{int(activity_id)}")


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Sommeil", **READ_ONLY))
async def garmin_sleep(
    start_date: str | None = None, end_date: str | None = None, detail: bool = False
) -> str:
    """Sommeil par nuit (durées en minutes, phases, score, coucher et lever). Défaut : 7 derniers
    jours. `detail=true` renvoie la réponse Garmin complète (volumineuse)."""
    start, end = _dates(start_date, end_date, 7)
    return await _call(
        "/sleep-service/sleep/dailySleepSummariesByDate",
        {"startDate": start, "endDate": end},
        slim=None if detail else slim_sleep,
    )


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Résumés quotidiens", **READ_ONLY))
async def garmin_daily_summaries(
    start_date: str | None = None, end_date: str | None = None, detail: bool = False
) -> str:
    """Résumé par jour : pas, calories, FC de repos, stress, minutes d'intensité, Body Battery.
    Défaut : 7 jours. `detail=true` renvoie la réponse Garmin complète (volumineuse)."""
    start, end = _dates(start_date, end_date, 7)
    return await _call(
        "/usersummary-service/v2/dailysummaries",
        {"startDate": start, "endDate": end},
        slim=None if detail else slim_daily,
    )


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="VFC (HRV)", **READ_ONLY))
async def garmin_hrv(start_date: str | None = None, end_date: str | None = None) -> str:
    """Résumés quotidiens de variabilité de fréquence cardiaque. Défaut : 7 derniers jours."""
    start, end = _dates(start_date, end_date, 7)
    return await _call(f"/hrv-service/hrv/daily/{start}/{end}")


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Body Battery", **READ_ONLY))
async def garmin_body_battery(
    start_date: str | None = None, end_date: str | None = None
) -> str:
    """Body Battery par jour. Défaut : 7 derniers jours."""
    start, end = _dates(start_date, end_date, 7)
    return await _call(
        "/wellness-service/wellness/bodyBattery/reports/daily",
        {"startDate": start, "endDate": end},
    )


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Appel Garmin libre (GET)", **READ_ONLY))
async def garmin_get(
    path: str, params: dict[str, str] | None = None, full: bool = False
) -> str:
    """GET sur n'importe quel chemin de l'API Garmin Personal, par exemple
    /usersummary-service/stats/steps/daily/2026-11-01/2026-11-07,
    /wellness-service/wellness/dailyHeartRate (params: date),
    /biometric-service/heartRateZones/v2, /userprofile-service/userprofile/settings.
    `full=true` désactive la troncature des longues listes (réponses volumineuses)."""
    if not path.startswith("/") or "://" in path or ".." in path or "\\" in path:
        return _dump({"erreur": "path doit être un chemin relatif commençant par /"})
    return await _call(path, params, full=full)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Any) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# --------------------------------------------------------------------------- Sécurité


class BearerAuth:
    """Refuse toute requête sans le bon jeton, sauf /healthz."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._expected = f"Bearer {MCP_AUTH_TOKEN}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        provided = dict(scope["headers"]).get(b"authorization", b"")
        if not hmac.compare_digest(provided, self._expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


app = BearerAuth(
    mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
        # Derrière Traefik, l'en-tête Host est le domaine public : la protection
        # anti-rebinding DNS (pensée pour localhost) est remplacée par le jeton.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=True, forwarded_allow_ips="*")
