#!/usr/bin/env python3
"""
Pega-Salesforce MCP Proxy
Roteia chamadas do Pega agent para Salesforce via OAuth
"""

import os
import json
import requests
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import uvicorn

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Pega-Salesforce MCP Proxy")

# Configuration from environment variables
SF_CLIENT_ID = os.getenv("SF_CLIENT_ID")
SF_CLIENT_SECRET = os.getenv("SF_CLIENT_SECRET")
SF_ORG_URL = os.getenv("SF_ORG_URL", "https://mbmconsulting-dev-ed.develop.my.salesforce.com")

# In-memory token cache (for this example)
_token_cache = {
    "access_token": None,
    "expires_at": None
}


def get_access_token() -> str:
    """Get or refresh Salesforce access token using Client Credentials flow"""

    # Check if we have a valid cached token
    if _token_cache["access_token"] and _token_cache["expires_at"]:
        if datetime.utcnow() < _token_cache["expires_at"]:
            logger.info("Using cached access token")
            return _token_cache["access_token"]

    logger.info("Requesting new access token from Salesforce")

    token_url = f"{SF_ORG_URL}/services/oauth2/token"

    # Use Client Credentials grant type
    payload = {
        "grant_type": "client_credentials",
        "client_id": SF_CLIENT_ID,
        "client_secret": SF_CLIENT_SECRET,
    }

    try:
        response = requests.post(token_url, data=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Token request failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to obtain Salesforce token")

    data = response.json()

    if "error" in data:
        logger.error(f"OAuth error: {data.get('error')} - {data.get('error_description')}")
        raise HTTPException(status_code=503, detail=f"OAuth error: {data['error']}")

    access_token = data.get("access_token")
    expires_in = data.get("expires_in", 3600)

    # Cache the token
    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = datetime.utcnow() + timedelta(seconds=expires_in - 60)

    logger.info(f"New access token obtained, expires in {expires_in}s")
    return access_token


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
