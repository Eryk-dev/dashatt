"""
MeLi Faturamento Sync

Polls MercadoLivre paid orders every N minutes and upserts
the daily total into the faturamento table (Supabase 141Air).
The dashboard picks up changes via its existing real-time subscription.

MeLi API refs:
  Auth:   POST https://api.mercadolibre.com/oauth/token
  Orders: GET  https://api.mercadolibre.com/orders/search
    params:  seller, order.status, order.date_created.from/to, sort, limit, offset
    response: { paging: {total, offset, limit}, results: [Order] }
    Order:    { id, status, total_amount, paid_amount, currency_id, date_created, date_closed, tags[], ... }
    paid_amount = produto + frete (total pago pelo comprador)
    total_amount = só produto (sem frete)
    Status:   confirmed | payment_required | payment_in_process | partially_paid | paid | cancelled | invalid
    Tags:     pode conter "fraud_risk_detected" → ignorar

MeLi Token lifecycle (https://developers.mercadolivre.com.br/en_us/authentication-and-authorization):
  - refresh_token é de USO ÚNICO — após usar, vira inválido
  - A resposta sempre traz um NOVO refresh_token que precisa ser salvo
  - refresh_token expira em 6 meses se não usado
  - access_token dura 6 horas

Supabase upsert via PostgREST:
  POST /rest/v1/faturamento  +  Prefer: resolution=merge-duplicates
  Tabela: faturamento (empresa TEXT, data DATE, valor NUMERIC) UNIQUE(empresa, data)
  Tabela: meli_tokens (account_name TEXT PK, refresh_token TEXT, access_token TEXT, ...)
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("meli-sync")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BRT = timezone(timedelta(hours=-3))

SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL_MINUTES", "5"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

MELI_AUTH_URL = "https://api.mercadolibre.com/oauth/token"
MELI_ORDERS_URL = "https://api.mercadolibre.com/orders/search"


def _load_accounts() -> list[dict]:
    """
    Auto-discover MeLi accounts from env vars.
    Pattern:  MELI_{NAME}_APP_ID, MELI_{NAME}_SECRET_KEY,
              MELI_{NAME}_REFRESH_TOKEN, MELI_{NAME}_USER_ID
    Mapping:  ACCOUNT_{NAME}_EMPRESA  (default = NAME)

    refresh_token from env is only used as initial seed.
    At runtime, the latest token is loaded from Supabase meli_tokens table.
    """
    names: set[str] = set()
    for key in os.environ:
        if key.startswith("MELI_") and key.endswith("_APP_ID"):
            names.add(key[5:-7])

    accounts = []
    for name in sorted(names):
        app_id = os.getenv(f"MELI_{name}_APP_ID")
        secret = os.getenv(f"MELI_{name}_SECRET_KEY")
        refresh = os.getenv(f"MELI_{name}_REFRESH_TOKEN")
        user_id = os.getenv(f"MELI_{name}_USER_ID")
        empresa = os.getenv(f"ACCOUNT_{name}_EMPRESA", name)

        if all([app_id, secret, refresh, user_id]):
            accounts.append(
                {
                    "name": name,
                    "empresa": empresa,
                    "app_id": app_id,
                    "secret_key": secret,
                    "refresh_token": refresh,
                    "user_id": user_id,
                }
            )
            log.info("Account loaded: %s → empresa '%s' (user %s)", name, empresa, user_id)
        else:
            log.warning("Account %s has incomplete credentials, skipping", name)

    return accounts


ACCOUNTS = _load_accounts()

# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

_supabase_headers: dict = {}


def _sb_headers() -> dict:
    if not _supabase_headers:
        _supabase_headers.update(
            {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
            }
        )
    return _supabase_headers


# ---------------------------------------------------------------------------
# Token persistence (Supabase meli_tokens table)
# ---------------------------------------------------------------------------


async def _load_token_from_db(account_name: str, client: httpx.AsyncClient) -> str | None:
    """Load the latest refresh_token from Supabase for this account."""
    res = await client.get(
        f"{SUPABASE_URL}/rest/v1/meli_tokens",
        params={"account_name": f"eq.{account_name}", "select": "refresh_token"},
        headers=_sb_headers(),
    )
    if res.status_code == 200:
        rows = res.json()
        if rows and rows[0].get("refresh_token"):
            return rows[0]["refresh_token"]
    return None


async def _save_token_to_db(
    account_name: str,
    refresh_token: str,
    access_token: str | None,
    client: httpx.AsyncClient,
):
    """Persist the new refresh_token (and access_token) to Supabase."""
    payload = {
        "account_name": account_name,
        "refresh_token": refresh_token,
        "updated_at": datetime.now(BRT).isoformat(),
    }
    if access_token:
        payload["access_token"] = access_token
        payload["access_token_expires_at"] = (
            datetime.now(BRT) + timedelta(hours=6)
        ).isoformat()

    res = await client.post(
        f"{SUPABASE_URL}/rest/v1/meli_tokens",
        json=payload,
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
    )
    if res.status_code >= 300:
        log.error("Failed to save token for %s: %s %s", account_name, res.status_code, res.text)


async def _load_all_tokens(client: httpx.AsyncClient):
    """On startup, load latest refresh_tokens from Supabase into ACCOUNTS."""
    for account in ACCOUNTS:
        db_token = await _load_token_from_db(account["name"], client)
        if db_token:
            account["refresh_token"] = db_token
            log.info("[%s] Refresh token loaded from Supabase", account["name"])
        else:
            log.info("[%s] Using refresh token from env (no DB entry yet)", account["name"])


# ---------------------------------------------------------------------------
# MeLi API
# ---------------------------------------------------------------------------


async def _get_access_token(account: dict, client: httpx.AsyncClient) -> str | None:
    """
    Exchange refresh_token for access_token.
    MeLi refresh tokens are SINGLE-USE — the response contains a new one
    that must be persisted immediately.
    """
    res = await client.post(
        MELI_AUTH_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": account["app_id"],
            "client_secret": account["secret_key"],
            "refresh_token": account["refresh_token"],
        },
    )
    if res.status_code != 200:
        log.error("[%s] Token refresh failed: %s %s", account["name"], res.status_code, res.text)
        return None

    data = res.json()
    access_token = data.get("access_token")
    new_refresh = data.get("refresh_token")

    if new_refresh:
        # Update in memory immediately
        account["refresh_token"] = new_refresh
        # Persist to Supabase so it survives restarts
        await _save_token_to_db(account["name"], new_refresh, access_token, client)
        log.info("[%s] New refresh token saved to Supabase", account["name"])

    return access_token


async def _fetch_paid_orders_total(
    account: dict, access_token: str, date_str: str, client: httpx.AsyncClient
) -> dict:
    """
    Paginate GET /orders/search and sum total_amount of paid orders.
    Skips orders tagged fraud_risk_detected.
    """
    date_from = f"{date_str}T00:00:00.000-03:00"
    date_to = f"{date_str}T23:59:59.999-03:00"

    total_valor = 0.0
    order_count = 0
    fraud_count = 0
    offset = 0
    limit = 50

    while True:
        res = await client.get(
            MELI_ORDERS_URL,
            params={
                "seller": account["user_id"],
                "order.status": "paid",
                "order.date_created.from": date_from,
                "order.date_created.to": date_to,
                "sort": "date_desc",
                "limit": limit,
                "offset": offset,
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if res.status_code != 200:
            log.error("[%s] Orders search failed at offset %d: %s", account["name"], offset, res.text)
            break

        data = res.json()
        for order in data.get("results", []):
            if "fraud_risk_detected" in (order.get("tags") or []):
                fraud_count += 1
                continue
            total_valor += order.get("paid_amount", 0) or order.get("total_amount", 0)
            order_count += 1

        total_results = data.get("paging", {}).get("total", 0)
        offset += limit
        if offset >= total_results or offset > 500:
            break

    return {
        "valor": round(total_valor, 2),
        "order_count": order_count,
        "fraud_skipped": fraud_count,
    }


# ---------------------------------------------------------------------------
# Supabase upsert
# ---------------------------------------------------------------------------


async def _upsert_faturamento(
    empresa: str, date_str: str, valor: float, client: httpx.AsyncClient
) -> bool:
    res = await client.post(
        f"{SUPABASE_URL}/rest/v1/faturamento?on_conflict=empresa,data",
        json={"empresa": empresa, "data": date_str, "valor": valor},
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    ok = 200 <= res.status_code < 300
    if not ok:
        log.error("Supabase upsert failed %s/%s: %s %s", empresa, date_str, res.status_code, res.text)
    return ok


# ---------------------------------------------------------------------------
# Sync orchestrator
# ---------------------------------------------------------------------------

_last_sync: str | None = None
_last_results: list[dict] = []


async def sync_all() -> list[dict]:
    global _last_sync, _last_results

    now_brt = datetime.now(BRT)
    date_str = now_brt.strftime("%Y-%m-%d")
    results: list[dict] = []

    log.info("Sync starting for %s (%d accounts)", date_str, len(ACCOUNTS))

    async with httpx.AsyncClient(timeout=30) as client:
        for account in ACCOUNTS:
            try:
                token = await _get_access_token(account, client)
                if not token:
                    results.append({"empresa": account["empresa"], "date": date_str, "status": "token_error"})
                    continue

                data = await _fetch_paid_orders_total(account, token, date_str, client)

                if data["valor"] > 0:
                    ok = await _upsert_faturamento(account["empresa"], date_str, data["valor"], client)
                    status = "synced" if ok else "upsert_error"
                else:
                    status = "no_sales"

                result = {
                    "empresa": account["empresa"],
                    "date": date_str,
                    "valor": data["valor"],
                    "orders": data["order_count"],
                    "fraud_skipped": data["fraud_skipped"],
                    "status": status,
                }
                results.append(result)
                log.info(
                    "[%s] %s: R$ %.2f (%d orders)",
                    account["name"],
                    status,
                    data["valor"],
                    data["order_count"],
                )

            except Exception as e:
                log.exception("[%s] Sync failed", account["name"])
                results.append({"empresa": account["empresa"], "date": date_str, "status": "error", "error": str(e)})

    _last_sync = now_brt.isoformat()
    _last_results = results
    log.info("Sync complete: %d accounts", len(results))
    return results


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------


async def _scheduler():
    # Load latest tokens from Supabase before first sync
    async with httpx.AsyncClient(timeout=30) as client:
        await _load_all_tokens(client)

    await sync_all()
    while True:
        await asyncio.sleep(SYNC_INTERVAL * 60)
        try:
            await sync_all()
        except Exception:
            log.exception("Scheduler error")


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_scheduler())
    yield
    task.cancel()


app = FastAPI(title="MeLi Faturamento Sync", lifespan=lifespan)


@app.get("/")
async def root():
    return {
        "service": "meli-faturamento-sync",
        "accounts": [a["empresa"] for a in ACCOUNTS],
        "interval_minutes": SYNC_INTERVAL,
        "last_sync": _last_sync,
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "accounts": len(ACCOUNTS), "last_sync": _last_sync}


@app.post("/sync")
async def manual_sync():
    """Trigger sync manually (same as the scheduled one)."""
    results = await sync_all()
    return {"results": results}


@app.get("/last")
async def last_results():
    return {"last_sync": _last_sync, "results": _last_results}
