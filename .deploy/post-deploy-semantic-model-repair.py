"""
Post-deployment Semantic Model repair for Microsoft Fabric CI/CD.

Handles:
    - Import
    - DirectQuery
    - Direct Lake
    - Mixed / composite models

Behaviour:
    Import:
        Repair Sql.Database references
        Update definition
        Take over model
        Trigger full refresh

    DirectQuery:
        Repair Sql.Database references
        Update definition
        Take over model
        No Import data refresh

    Direct Lake:
        Inspect and repair environment-specific references found in TMDL
        Update definition when changes are required
        Take over model
        No Import data refresh

    Mixed model:
        Repair all detected applicable references.
        Trigger refresh if Import partitions are present.

Runs on Azure DevOps agent using Service Principal authentication.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from azure.identity import ClientSecretCredential


# =========================================================================
# CONFIGURATION
# =========================================================================

FABRIC_API = "https://api.fabric.microsoft.com/v1"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

REQUEST_TIMEOUT = 60

SQL_ENDPOINT_MAX_ATTEMPTS = 30
LRO_MAX_ATTEMPTS = 60

REFRESH_MAX_ATTEMPTS = 120
REFRESH_POLL_SECONDS = 6


# =========================================================================
# AUTHENTICATION
# =========================================================================

class TokenManager:

    def __init__(self, credential):

        self.credential = credential


    def get_fabric_headers(self):

        token = self.credential.get_token(
            FABRIC_SCOPE
        )

        return {
            "Authorization":
                f"Bearer {token.token}",

            "Content-Type":
                "application/json",
        }


    def get_powerbi_headers(self):

        token = self.credential.get_token(
            POWERBI_SCOPE
        )

        return {
            "Authorization":
                f"Bearer {token.token}",

            "Content-Type":
                "application/json",
        }


# =========================================================================
# HTTP
# =========================================================================

def api_request(
    method,
    url,
    headers,
    **kwargs
):

    kwargs.setdefault(
        "timeout",
        REQUEST_TIMEOUT
    )

    return requests.request(
        method,
        url,
        headers=headers,
        **kwargs
    )


# =========================================================================
# WORKSPACE
# =========================================================================

def get_workspace_id(
    workspace_name,
    headers
):

    response = api_request(
        "GET",
        f"{FABRIC_API}/workspaces",
        headers
    )

    response.raise_for_status()

    workspaces = response.json().get(
        "value",
        []
    )

    matches = [
        workspace
        for workspace
        in workspaces

        if workspace.get(
            "displayName"
        ) == workspace_name
    ]

    if not matches:

        raise ValueError(
            f"Workspace "
            f"'{workspace_name}' "
            f"not found."
        )


    if len(matches) > 1:

        raise ValueError(
            f"Multiple workspaces named "
            f"'{workspace_name}' found."
        )


    return matches[0]["id"]


# =========================================================================
# LIST FABRIC ITEMS
# =========================================================================

def list_items(
    workspace_id,
    item_type,
    headers
):

    items = []

    url = (
        f"{FABRIC_API}/workspaces/"
        f"{workspace_id}/items"
        f"?type={item_type}"
    )


    while url:

        response = api_request(
            "GET",
            url,
            headers
        )

        response.raise_for_status()

        body = response.json()

        items.extend(
            body.get(
                "value",
                []
            )
        )


        continuation_uri = body.get(
            "continuationUri"
        )

        continuation_token = body.get(
            "continuationToken"
        )


        if continuation_uri:

            url = continuation_uri


        elif continuation_token:

            url = (
                f"{FABRIC_API}/workspaces/"
                f"{workspace_id}/items"
                f"?type={item_type}"
                f"&continuationToken="
                f"{continuation_token}"
            )


        else:

            url = None


    return items


# =========================================================================
# LAKEHOUSE SQL ENDPOINT
# =========================================================================

def get_lakehouse_sql_endpoint(
    workspace_id,
    lakehouse_id,
    headers
):

    url = (
        f"{FABRIC_API}/workspaces/"
        f"{workspace_id}/lakehouses/"
        f"{lakehouse_id}"
    )


    for attempt in range(
        1,
        SQL_ENDPOINT_MAX_ATTEMPTS + 1
    ):

        response = api_request(
            "GET",
            url,
            headers
        )

        response.raise_for_status()


        properties = (
            response
            .json()
            .get(
                "properties",
                {}
            )
        )


        sql_properties = (
            properties.get(
                "sqlEndpointProperties",
                {}
            )
        )


        status = sql_properties.get(
            "provisioningStatus"
        )


        if status == "Success":

            server = sql_properties.get(
                "connectionString"
            )

            database = sql_properties.get(
                "id"
            )


            if not server or not database:

                raise RuntimeError(
                    "SQL endpoint provisioned "
                    "but server/database "
                    "information is missing."
                )


            return {
                "server": server,
                "database": database
            }


        print(
            f"  SQL endpoint status: "
            f"{status or 'Unknown'} "
            f"attempt "
            f"{attempt}/"
            f"{SQL_ENDPOINT_MAX_ATTEMPTS}"
        )

        time.sleep(10)


    raise TimeoutError(
        "Lakehouse SQL endpoint "
        "did not provision."
    )


# =========================================================================
# LONG RUNNING OPERATION
# =========================================================================

def poll_lro(
    location,
    headers
):

    if not location:

        raise RuntimeError(
            "Fabric returned 202 "
            "without Location header."
        )


    retry_after = 5


    for _ in range(
        LRO_MAX_ATTEMPTS
    ):

        time.sleep(
            retry_after
        )


        response = api_request(
            "GET",
            location,
            headers
        )


        if response.status_code == 429:

            retry_after = int(
                response.headers.get(
                    "Retry-After",
                    retry_after
                )
            )

            continue


        response.raise_for_status()

        result = response.json()


        retry_after = int(
            response.headers.get(
                "Retry-After",
                retry_after
            )
        )


        status = result.get(
            "status"
        )


        if status == "Succeeded":

            return (
                result.get(
                    "resultUrl"
                )

                or

                f"{location.rstrip('/')}"
                f"/result"
            )


        if status == "Failed":

            raise RuntimeError(
                "Fabric LRO failed: "
                + json.dumps(
                    result.get(
                        "error",
                        result
                    )
                )
            )


    raise TimeoutError(
        "Fabric LRO timed out."
    )


# =========================================================================
# SEMANTIC MODEL DEFINITION
# =========================================================================

def get_semantic_model_definition(
    workspace_id,
    semantic_model_id,
    headers
):

    url = (
        f"{FABRIC_API}/workspaces/"
        f"{workspace_id}/semanticModels/"
        f"{semantic_model_id}/"
        f"getDefinition?format=TMDL"
    )


    response = api_request(
        "POST",
        url,
        headers
    )


    if response.status_code == 202:

        result_url = poll_lro(
            response.headers.get(
                "Location",
                ""
            ),
            headers
        )


        response = api_request(
            "GET",
            result_url,
            headers
        )


    response.raise_for_status()

    result = response.json()

    definition = result.get(
        "definition"
    )


    if not definition:

        raise RuntimeError(
            "Semantic model definition "
            "not returned."
        )


    return definition


# =========================================================================
# UPDATE SEMANTIC MODEL
# =========================================================================

def update_semantic_model_definition(
    workspace_id,
    semantic_model_id,
    definition,
    headers
):

    url = (
        f"{FABRIC_API}/workspaces/"
        f"{workspace_id}/semanticModels/"
        f"{semantic_model_id}/"
        f"updateDefinition"
    )


    response = api_request(
        "POST",
        url,
        headers,
        json={
            "definition":
                definition
        }
    )


    if response.status_code == 202:

        poll_lro(
            response.headers.get(
                "Location",
                ""
            ),
            headers
        )

        return


    response.raise_for_status()


# =========================================================================
# TMDL HELPERS
# =========================================================================

def decode_part(
    part
):

    payload = part.get(
        "payload"
    )


    if not payload:

        return ""


    return (
        base64
        .b64decode(
            payload
        )
        .decode(
            "utf-8"
        )
    )


def encode_part(
    part,
    content
):

    part["payload"] = (
        base64
        .b64encode(
            content.encode(
                "utf-8"
            )
        )
        .decode(
            "utf-8"
        )
    )

    part[
        "payloadType"
    ] = "InlineBase64"


def get_tmdl_parts(
    definition
):

    parts = []


    for part in definition.get(
        "parts",
        []
    ):

        path = part.get(
            "path",
            ""
        )


        if path.lower().endswith(
            ".tmdl"
        ):

            parts.append(
                (
                    part,
                    decode_part(
                        part
                    )
                )
            )


    return parts


# =========================================================================
# DETECT STORAGE MODES
# =========================================================================

def detect_storage_modes(
    definition
):

    """
    Detect all partition modes.

    Possible modes include:

        import
        directQuery
        directLake

    A model may contain more than one mode.
    """

    modes = {}


    pattern = re.compile(
        r"(?im)"
        r"^\s*mode\s*:\s*"
        r"([A-Za-z]+)"
        r"\s*$"
    )


    for part, content in get_tmdl_parts(
        definition
    ):

        path = (
            part.get(
                "path",
                ""
            )
            .lower()
        )


        if "tables/" not in path:

            continue


        for match in pattern.finditer(
            content
        ):

            mode = (
                match
                .group(1)
                .strip()
                .lower()
            )


            modes[mode] = (
                modes.get(
                    mode,
                    0
                )
                + 1
            )


    return modes


# =========================================================================
# DETERMINE MODEL TYPE
# =========================================================================

def classify_model(
    storage_modes
):

    """
    Return model classification.

    IMPORT
    DIRECTQUERY
    DIRECTLAKE
    MIXED
    UNKNOWN
    """


    if not storage_modes:

        return "UNKNOWN"


    active_modes = {
        mode
        for mode, count
        in storage_modes.items()
        if count > 0
    }


    if active_modes == {
        "import"
    }:

        return "IMPORT"


    if active_modes == {
        "directquery"
    }:

        return "DIRECTQUERY"


    if active_modes == {
        "directlake"
    }:

        return "DIRECTLAKE"


    return "MIXED"


# =========================================================================
# SQL CONNECTION REPAIR
# =========================================================================

def build_sql_replacements(
    definition,
    target_endpoint
):

    """
    Find Sql.Database references.

    Applies to Import and DirectQuery
    models when the source definition
    contains Sql.Database().
    """

    replacements = {}


    sql_pattern = re.compile(
        r'Sql\.Database\('
        r'\s*"([^"]+)"'
        r'\s*,\s*'
        r'"([^"]+)"',
        re.IGNORECASE
    )


    for _, content in get_tmdl_parts(
        definition
