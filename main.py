#!/usr/bin/env python3
"""
Pega-Salesforce MCP Proxy
Roteia chamadas do Pega agent para Salesforce via OAuth
Implements MCP (Model Context Protocol) 2025-03-26
"""

import os
import json
import requests
import logging
from datetime import datetime, timedelta
from typing import Optional, Any, Dict, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Pega-Salesforce MCP Proxy")

# Add CORS middleware for Pega compatibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Google Secret Manager (Primary) - fallback to env vars
def get_secret(secret_name: str) -> Optional[str]:
    """Retrieve secret from Google Secret Manager first, then environment variable as fallback"""

    # Try Secret Manager FIRST (primary source)
    try:
        from google.cloud import secretmanager
        project_id = os.getenv("GCP_PROJECT_ID")
        if project_id:
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
            response = client.access_secret_version(request={"name": name})
            secret_value = response.payload.data.decode("UTF-8")
            logger.info(f"✅ Loaded {secret_name} from Secret Manager")
            return secret_value
    except Exception as e:
        logger.debug(f"Secret Manager fallback for {secret_name}: {e}")

    # Fallback to environment variable
    env_key = f"SF_{secret_name.upper()}"
    if os.getenv(env_key):
        logger.info(f"⚠️  Loaded {secret_name} from env var (Secret Manager unavailable)")
        return os.getenv(env_key)

    logger.warning(f"❌ Secret {secret_name} not found in Secret Manager or env vars")
    return None

# Configuration - Secret Manager primary, fallback to env vars
SF_CLIENT_ID = get_secret("sf-client-id") or os.getenv("SF_CLIENT_ID")
SF_CLIENT_SECRET = get_secret("sf-client-secret") or os.getenv("SF_CLIENT_SECRET")
SF_ORG_URL = os.getenv("SF_ORG_URL", "https://mbmconsulting-dev-ed.develop.my.salesforce.com")
SF_ACCESS_TOKEN = get_secret("sf-access-token") or os.getenv("SF_ACCESS_TOKEN")
SF_REFRESH_TOKEN = get_secret("sf-refresh-token") or os.getenv("SF_REFRESH_TOKEN")

logger.info(f"DEBUG: ENV SF_CLIENT_ID={os.getenv('SF_CLIENT_ID')[:30] if os.getenv('SF_CLIENT_ID') else None}...")
logger.info(f"DEBUG: ENV SF_REFRESH_TOKEN={os.getenv('SF_REFRESH_TOKEN')[:30] if os.getenv('SF_REFRESH_TOKEN') else None}...")
logger.info(f"✅ Config loaded: CLIENT_ID={bool(SF_CLIENT_ID)}, TOKENS={bool(SF_ACCESS_TOKEN and SF_REFRESH_TOKEN)}")

# MCP Protocol version
MCP_PROTOCOL_VERSION = "2025-03-26"

# In-memory token cache (for this example)
_token_cache = {
    "access_token": SF_ACCESS_TOKEN,
    "refresh_token": SF_REFRESH_TOKEN,
    "expires_at": None
}


def get_access_token() -> str:
    """Get or refresh Salesforce access token using Authorization Code Flow with refresh token"""

    # Check if we have a valid cached token
    if _token_cache["access_token"] and _token_cache["expires_at"]:
        if datetime.utcnow() < _token_cache["expires_at"]:
            logger.info("Using cached access token")
            return _token_cache["access_token"]

    logger.info("Refreshing access token from Salesforce")

    if not SF_CLIENT_ID or not SF_CLIENT_SECRET or not _token_cache["refresh_token"]:
        raise ValueError("SF_CLIENT_ID, SF_CLIENT_SECRET, and SF_REFRESH_TOKEN environment variables required")

    token_url = f"{SF_ORG_URL}/services/oauth2/token"

    # Use refresh_token grant type
    payload = {
        "grant_type": "refresh_token",
        "client_id": SF_CLIENT_ID,
        "client_secret": SF_CLIENT_SECRET,
        "refresh_token": _token_cache["refresh_token"],
    }

    try:
        response = requests.post(token_url, data=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Token refresh failed: {e}")
        raise ValueError(f"Failed to obtain Salesforce token: {str(e)}")

    data = response.json()

    if "error" in data:
        error_desc = data.get('error_description', data.get('error'))
        logger.error(f"OAuth error: {data.get('error')} - {error_desc}")
        raise ValueError(f"OAuth error: {data.get('error')} - {error_desc}")

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token", _token_cache["refresh_token"])
    expires_in = data.get("expires_in", 3600)

    if not access_token:
        raise ValueError("No access_token in Salesforce response")

    # Cache the new tokens
    _token_cache["access_token"] = access_token
    _token_cache["refresh_token"] = refresh_token
    _token_cache["expires_at"] = datetime.utcnow() + timedelta(seconds=expires_in - 60)

    logger.info(f"New access token obtained, expires in {expires_in}s")
    return access_token


async def _call_soql_query(query: str) -> dict:
    """Execute SOQL query"""
    if not query:
        raise ValueError("query parameter is required")

    access_token = get_access_token()
    query_url = f"{SF_ORG_URL}/services/data/v62.0/query"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    params = {"q": query}

    response = requests.get(query_url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


async def _call_get_sobject(sobject_type: str, record_id: str) -> dict:
    """Get a single Salesforce record"""
    if not sobject_type or not record_id:
        raise ValueError("sobjectType and recordId are required")

    access_token = get_access_token()
    url = f"{SF_ORG_URL}/services/data/v62.0/sobjects/{sobject_type}/{record_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


async def _call_create_sobject(sobject_type: str, data: dict) -> dict:
    """Create a new Salesforce record"""
    if not sobject_type or not data:
        raise ValueError("sobjectType and data are required")

    access_token = get_access_token()
    url = f"{SF_ORG_URL}/services/data/v62.0/sobjects/{sobject_type}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    response = requests.post(url, headers=headers, json=data, timeout=10)
    response.raise_for_status()
    return response.json()


async def _call_update_sobject(sobject_type: str, record_id: str, data: dict) -> dict:
    """Update a Salesforce record"""
    if not sobject_type or not record_id or not data:
        raise ValueError("sobjectType, recordId, and data are required")

    access_token = get_access_token()
    url = f"{SF_ORG_URL}/services/data/v62.0/sobjects/{sobject_type}/{record_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    response = requests.patch(url, headers=headers, json=data, timeout=10)
    response.raise_for_status()

    if response.status_code == 204:
        return {"success": True, "message": "Record updated"}

    return response.json()


async def _call_delete_sobject(sobject_type: str, record_id: str) -> dict:
    """Delete a Salesforce record"""
    if not sobject_type or not record_id:
        raise ValueError("sobjectType and recordId are required")

    access_token = get_access_token()
    url = f"{SF_ORG_URL}/services/data/v62.0/sobjects/{sobject_type}/{record_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    response = requests.delete(url, headers=headers, timeout=10)
    response.raise_for_status()
    return {"success": True, "status_code": response.status_code}


async def _call_get_limits() -> dict:
    """Get organization limits"""
    access_token = get_access_token()
    url = f"{SF_ORG_URL}/services/data/v62.0/limits"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "ok",
        "has_client_id": bool(SF_CLIENT_ID),
        "has_client_secret": bool(SF_CLIENT_SECRET),
        "has_access_token": bool(SF_ACCESS_TOKEN),
        "has_refresh_token": bool(SF_REFRESH_TOKEN),
        "cache_state": {
            "access_token": bool(_token_cache.get("access_token")),
            "refresh_token": bool(_token_cache.get("refresh_token"))
        }
    }

@app.post("/debug-refresh")
async def debug_refresh() -> JSONResponse:
    """Debug endpoint - shows actual error"""
    try:
        token_url = f"{SF_ORG_URL}/services/oauth2/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": SF_CLIENT_ID,
            "client_secret": SF_CLIENT_SECRET,
            "refresh_token": _token_cache.get("refresh_token"),
        }

        logger.info(f"🔍 DEBUG: URL={token_url}, payload keys={list(payload.keys())}")
        response = requests.post(token_url, data=payload, timeout=30)
        response_text = response.text

        logger.info(f"Response status={response.status_code}, body={response_text[:200]}")

        return JSONResponse({
            "url": token_url,
            "status_code": response.status_code,
            "response": response_text[:500]
        })
    except Exception as e:
        return JSONResponse({
            "error": str(e),
            "type": type(e).__name__
        }, status_code=500)

@app.post("/")
@app.post("/refresh-tokens")
async def refresh_tokens_endpoint() -> JSONResponse:
    """Endpoint para renovar tokens Salesforce via Cloud Scheduler"""
    try:
        logger.info("🔄 Token refresh requested via endpoint")

        current_refresh_token = _token_cache.get("refresh_token")
        if not current_refresh_token:
            return JSONResponse(
                {"status": "error", "message": "No refresh token available"},
                status_code=400
            )

        token_url = f"{SF_ORG_URL}/services/oauth2/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": SF_CLIENT_ID,
            "client_secret": SF_CLIENT_SECRET,
            "refresh_token": current_refresh_token,
        }

        logger.info(f"Requesting new tokens from Salesforce...")
        response = requests.post(token_url, data=payload, timeout=30)
        response.raise_for_status()

        data = response.json()

        if "error" in data:
            error_msg = f"OAuth error: {data.get('error')} - {data.get('error_description')}"
            logger.error(error_msg)
            return JSONResponse(
                {"status": "error", "message": error_msg},
                status_code=400
            )

        new_access_token = data.get("access_token")
        new_refresh_token = data.get("refresh_token", current_refresh_token)

        if not new_access_token:
            return JSONResponse(
                {"status": "error", "message": "No access_token in response"},
                status_code=400
            )

        _token_cache["access_token"] = new_access_token
        _token_cache["refresh_token"] = new_refresh_token
        _token_cache["expires_at"] = datetime.utcnow() + timedelta(seconds=3600 - 60)

        logger.info("✅ Tokens refreshed and cached successfully")

        return JSONResponse({
            "status": "success",
            "message": "Tokens refreshed successfully",
            "timestamp": datetime.utcnow().isoformat(),
            "access_token_prefix": new_access_token[:20] + "...",
            "expires_in_seconds": 3600
        })

    except requests.exceptions.RequestException as e:
        error_msg = f"Token refresh failed: {str(e)}"
        logger.error(error_msg)
        return JSONResponse(
            {"status": "error", "message": error_msg},
            status_code=500
        )
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(error_msg)
        return JSONResponse(
            {"status": "error", "message": error_msg},
            status_code=500
        )


@app.post("/mcp")
async def mcp_handler(request: Request):
    """MCP Protocol 2025-03-26 JSON-RPC handler"""
    try:
        body = await request.json()
    except:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
            status_code=400
        )

    jsonrpc = body.get("jsonrpc", "2.0")
    method = body.get("method")
    params = body.get("params", {})
    req_id = body.get("id")

    logger.info(f"MCP RPC: {method}")

    try:
        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "pega-salesforce-mcp-proxy",
                        "version": "1.0.0"
                    }
                },
                "id": req_id
            }
        elif method == "tools/list":
            response = {
                "jsonrpc": "2.0",
                "result": {
                    "tools": [
                        {
                            "name": "soqlQuery",
                            "description": "Execute SOQL query on Salesforce",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "SOQL query string"
                                    }
                                },
                                "required": ["query"]
                            }
                        },
                        {
                            "name": "getSobjectRecord",
                            "description": "Get a single Salesforce record",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "sobjectType": {"type": "string", "description": "e.g. Account, Contact, Opportunity"},
                                    "recordId": {"type": "string", "description": "Salesforce record ID"}
                                },
                                "required": ["sobjectType", "recordId"]
                            }
                        },
                        {
                            "name": "createSobjectRecord",
                            "description": "Create a new Salesforce record",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "sobjectType": {"type": "string"},
                                    "data": {"type": "object", "description": "Fields to set"}
                                },
                                "required": ["sobjectType", "data"]
                            }
                        },
                        {
                            "name": "updateSobjectRecord",
                            "description": "Update a Salesforce record",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "sobjectType": {"type": "string"},
                                    "recordId": {"type": "string"},
                                    "data": {"type": "object"}
                                },
                                "required": ["sobjectType", "recordId", "data"]
                            }
                        },
                        {
                            "name": "deleteSobjectRecord",
                            "description": "Delete a Salesforce record",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "sobjectType": {"type": "string"},
                                    "recordId": {"type": "string"}
                                },
                                "required": ["sobjectType", "recordId"]
                            }
                        },
                        {
                            "name": "getOrgLimits",
                            "description": "Get Salesforce organization limits",
                            "inputSchema": {"type": "object"}
                        }
                    ]
                },
                "id": req_id
            }
        elif method == "resources/list":
            response = {
                "jsonrpc": "2.0",
                "result": {"resources": []},
                "id": req_id
            }
        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})

            logger.info(f"Calling tool: {tool_name} with args: {tool_args}")

            try:
                if tool_name == "soqlQuery":
                    result = await _call_soql_query(tool_args.get("query"))
                elif tool_name == "getSobjectRecord":
                    result = await _call_get_sobject(
                        tool_args.get("sobjectType"),
                        tool_args.get("recordId")
                    )
                elif tool_name == "createSobjectRecord":
                    result = await _call_create_sobject(
                        tool_args.get("sobjectType"),
                        tool_args.get("data")
                    )
                elif tool_name == "updateSobjectRecord":
                    result = await _call_update_sobject(
                        tool_args.get("sobjectType"),
                        tool_args.get("recordId"),
                        tool_args.get("data")
                    )
                elif tool_name == "deleteSobjectRecord":
                    result = await _call_delete_sobject(
                        tool_args.get("sobjectType"),
                        tool_args.get("recordId")
                    )
                elif tool_name == "getOrgLimits":
                    result = await _call_get_limits()
                else:
                    raise ValueError(f"Unknown tool: {tool_name}")

                response = {
                    "jsonrpc": "2.0",
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result, indent=2)
                            }
                        ]
                    },
                    "id": req_id
                }
            except Exception as tool_error:
                import traceback
                error_msg = str(tool_error) or type(tool_error).__name__
                error_trace = traceback.format_exc()
                logger.error(f"Tool execution error: {error_msg}\n{error_trace}")
                response = {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32603,
                        "message": f"Tool execution failed: {error_msg}"
                    },
                    "id": req_id
                }
        else:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"Method not found: {method}"},
                "id": req_id
            }

        logger.info(f"MCP Response: {response}")
        return JSONResponse(response)

    except Exception as e:
        logger.error(f"MCP error: {e}")
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}, "id": req_id},
            status_code=500
        )


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok", "service": "pega-salesforce-mcp-proxy"}


@app.post("/soql")
async def query_soql(query: dict):
    """Execute SOQL query on Salesforce"""

    soql = query.get("query")
    if not soql:
        raise HTTPException(status_code=400, detail="Missing 'query' parameter")

    access_token = get_access_token()

    # Execute SOQL query via REST API
    query_url = f"{SF_ORG_URL}/services/data/v62.0/query"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    params = {"q": soql}

    try:
        response = requests.get(query_url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"SOQL query failed: {e}")
        raise HTTPException(status_code=503, detail="Salesforce query failed")

    return response.json()


@app.get("/sobject/{sobject_type}/{record_id}")
async def get_sobject_record(sobject_type: str, record_id: str):
    """Get a single Salesforce record"""

    access_token = get_access_token()

    url = f"{SF_ORG_URL}/services/data/v62.0/sobjects/{sobject_type}/{record_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Get record failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to retrieve record")

    return response.json()


@app.post("/sobject/{sobject_type}")
async def create_sobject_record(sobject_type: str, record: dict):
    """Create a new Salesforce record"""

    access_token = get_access_token()

    url = f"{SF_ORG_URL}/services/data/v62.0/sobjects/{sobject_type}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, json=record, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Create record failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to create record")

    return response.json()


@app.patch("/sobject/{sobject_type}/{record_id}")
async def update_sobject_record(sobject_type: str, record_id: str, record: dict):
    """Update a Salesforce record"""

    access_token = get_access_token()

    url = f"{SF_ORG_URL}/services/data/v62.0/sobjects/{sobject_type}/{record_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = requests.patch(url, headers=headers, json=record, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Update record failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to update record")

    if response.status_code == 204:
        return {"success": True}

    return response.json()


@app.delete("/sobject/{sobject_type}/{record_id}")
async def delete_sobject_record(sobject_type: str, record_id: str):
    """Delete a Salesforce record"""

    access_token = get_access_token()

    url = f"{SF_ORG_URL}/services/data/v62.0/sobjects/{sobject_type}/{record_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.delete(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Delete record failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to delete record")

    return {"success": True, "status_code": response.status_code}


@app.get("/limits")
async def get_org_limits():
    """Get organization limits"""

    access_token = get_access_token()

    url = f"{SF_ORG_URL}/services/data/v62.0/limits"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Get limits failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to get org limits")

    return response.json()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
