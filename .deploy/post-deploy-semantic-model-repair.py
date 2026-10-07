"""
Post-deployment semantic model connection repair.

Purpose
-------
For semantic models deployed through fabric-cicd:

1. Resolve the target Fabric workspace.
2. Resolve the target Lakehouse SQL analytics endpoint.
3. Inspect semantic model TMDL.
4. Detect Import / Direct Lake / DirectQuery storage mode.
5. ONLY repair models containing Import-mode partitions.
6. Replace stale Sql.Database(server, database) references.
7. Update the semantic model definition.
8. Take over the semantic model.
9. Trigger and monitor a full refresh.

Direct Lake and DirectQuery-only semantic models are skipped.

Runs on an Azure DevOps agent using Service Principal authentication.
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


# -------------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------------

FABRIC_API = "https://api.fabric.microsoft.com/v1"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

REQUEST_TIMEOUT = 60
SQL_ENDPOINT_MAX_ATTEMPTS = 30
LRO_MAX_ATTEMPTS = 60
REFRESH_MAX_ATTEMPTS = 120
REFRESH_POLL_SECONDS = 6


# -------------------------------------------------------------------------
# Authentication
# -------------------------------------------------------------------------

class TokenManager:

    def __init__(self, credential):
        self.credential = credential

    def get_fabric_headers(self):
        token = self.credential.get_token(FABRIC_SCOPE)

        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    def get_powerbi_headers(self):
        token = self.credential.get_token(POWERBI_SCOPE)

        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }


# -------------------------------------------------------------------------
# HTTP wrapper
# -------------------------------------------------------------------------

def api_request(method, url, headers, **kwargs):

    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    return requests.request(
        method,
        url,
        headers=headers,
        **kwargs,
    )


# -------------------------------------------------------------------------
# Workspace
# -------------------------------------------------------------------------

def get_workspace_id(workspace_name, headers):

    response = api_request(
        "GET",
        f"{FABRIC_API}/workspaces",
        headers,
    )

    response.raise_for_status()

    workspaces = response.json().get("value", [])

    matches = [
        ws
        for ws in workspaces
        if ws.get("displayName") == workspace_name
    ]

    if not matches:
        raise ValueError(
            f"Workspace '{workspace_name}' not found."
        )

    if len(matches) > 1:
        raise ValueError(
            f"Multiple workspaces named '{workspace_name}' found."
        )

    return matches[0]["id"]


# -------------------------------------------------------------------------
# Fabric items
# -------------------------------------------------------------------------

def list_items(workspace_id, item_type, headers):

    url = (
        f"{FABRIC_API}/workspaces/"
        f"{workspace_id}/items?type={item_type}"
    )

    items = []

    while url:

        response = api_request(
            "GET",
            url,
            headers,
        )

        response.raise_for_status()

        body = response.json()

        items.extend(
            body.get("value", [])
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
                f"&continuationToken={continuation_token}"
            )

        else:

            url = None

    return items


# -------------------------------------------------------------------------
# Lakehouse SQL endpoint
# -------------------------------------------------------------------------

def get_lakehouse_sql_endpoint(
    workspace_id,
    lakehouse_id,
    headers,
):

    url = (
        f"{FABRIC_API}/workspaces/"
        f"{workspace_id}/lakehouses/"
        f"{lakehouse_id}"
    )

    for attempt in range(
        1,
        SQL_ENDPOINT_MAX_ATTEMPTS + 1,
    ):

        response = api_request(
            "GET",
            url,
            headers,
        )

        response.raise_for_status()

        sql_properties = (
            response
            .json()
            .get("properties", {})
            .get("sqlEndpointProperties", {})
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
                    "SQL endpoint provisioned but "
                    "server/database information is missing."
                )

            return {
                "server": server,
                "database": database,
            }

        print(
            f"  SQL endpoint status: "
            f"{status or 'Unknown'} "
            f"({attempt}/"
            f"{SQL_ENDPOINT_MAX_ATTEMPTS})"
        )

        time.sleep(10)

    raise TimeoutError(
        "Lakehouse SQL endpoint "
        "did not provision in time."
    )


# -------------------------------------------------------------------------
# Fabric Long Running Operations
# -------------------------------------------------------------------------

def poll_lro(location, headers):

    if not location:

        raise RuntimeError(
            "Fabric returned 202 "
            "without Location header."
        )

    retry_after = 5

    for _ in range(LRO_MAX_ATTEMPTS):

        time.sleep(retry_after)

        response = api_request(
            "GET",
            location,
            headers,
        )

        if response.status_code == 429:

            retry_after = int(
                response.headers.get(
                    "Retry-After",
                    retry_after,
                )
            )

            continue

        response.raise_for_status()

        result = response.json()

        retry_after = int(
            response.headers.get(
                "Retry-After",
                retry_after,
            )
        )

        status = result.get("status")

        if status == "Succeeded":

            return (
                result.get("resultUrl")
                or
                f"{location.rstrip('/')}/result"
            )

        if status == "Failed":

            raise RuntimeError(
                "Fabric LRO failed: "
                + json.dumps(
                    result.get(
                        "error",
                        result,
                    )
                )
            )

    raise TimeoutError(
        "Fabric LRO polling timed out."
    )


# -------------------------------------------------------------------------
# Semantic Model Definition
# -------------------------------------------------------------------------

def get_semantic_model_definition(
    workspace_id,
    semantic_model_id,
    headers,
):

    url = (
        f"{FABRIC_API}/workspaces/"
        f"{workspace_id}/semanticModels/"
        f"{semantic_model_id}/getDefinition"
        f"?format=TMDL"
    )

    response = api_request(
        "POST",
        url,
        headers,
    )

    if response.status_code == 202:

        result_url = poll_lro(
            response.headers.get(
                "Location",
                ""
            ),
            headers,
        )

        response = api_request(
            "GET",
            result_url,
            headers,
        )

    response.raise_for_status()

    result = response.json()

    definition = result.get(
        "definition"
    )

    if not definition:

        raise RuntimeError(
            "Semantic model definition "
            "was not returned."
        )

    return definition


def update_semantic_model_definition(
    workspace_id,
    semantic_model_id,
    definition,
    headers,
):

    url = (
        f"{FABRIC_API}/workspaces/"
        f"{workspace_id}/semanticModels/"
        f"{semantic_model_id}/updateDefinition"
    )

    response = api_request(
        "POST",
        url,
        headers,
        json={
            "definition": definition
        },
    )

    if response.status_code == 202:

        poll_lro(
            response.headers.get(
                "Location",
                ""
            ),
            headers,
        )

        return

    response.raise_for_status()


# -------------------------------------------------------------------------
# TMDL Helpers
# -------------------------------------------------------------------------

def decode_part(part):

    payload = part.get("payload")

    if not payload:
        return ""

    return (
        base64
        .b64decode(payload)
        .decode("utf-8")
    )


def encode_part(part, content):

    part["payload"] = (
        base64
        .b64encode(
            content.encode("utf-8")
        )
        .decode("utf-8")
    )

    part["payloadType"] = "InlineBase64"


def get_tmdl_parts(definition):

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
                    decode_part(part),
                )
            )

    return parts


# -------------------------------------------------------------------------
# Storage Mode Detection
# -------------------------------------------------------------------------

def detect_storage_modes(definition):

    """
    Detect semantic model partition storage modes.

    Typical values:

        mode: import
        mode: directQuery
        mode: directLake

    Returns:

        {
            "import": 5,
            "directlake": 0,
            "directquery": 0
        }
    """

    modes = {}

    pattern = re.compile(
        r"(?im)^\s*mode\s*:\s*([A-Za-z]+)\s*$"
    )

    for part, content in get_tmdl_parts(
        definition
    ):

        path = (
            part
            .get("path", "")
            .lower()
        )

        # Storage mode is relevant to table
        # partition definitions.
        if "tables/" not in path:
            continue

        for match in pattern.finditer(
            content
        ):

            mode = (
                match.group(1)
                .strip()
                .lower()
            )

            modes[mode] = (
                modes.get(mode, 0)
                + 1
            )

    return modes


def should_process_model(
    storage_modes,
    allow_mixed_mode=False,
):

    """
    Process only models containing Import partitions.
    """

    if not storage_modes:

        return (
            False,
            "No explicit partition "
            "storage mode detected."
        )

    import_count = storage_modes.get(
        "import",
        0
    )

    if import_count == 0:

        return (
            False,
            "No Import partitions found. "
            f"Detected: {storage_modes}"
        )

    non_import_modes = {
        mode: count
        for mode, count
        in storage_modes.items()
        if (
            mode != "import"
            and count > 0
        )
    }

    if (
        non_import_modes
        and not allow_mixed_mode
    ):

        return (
            False,
            "Mixed-mode model detected. "
            f"Detected: {storage_modes}"
        )

    return (
        True,
        "Import model detected. "
        f"Partitions: {storage_modes}"
    )


# -------------------------------------------------------------------------
# Connection Repair
# -------------------------------------------------------------------------

def build_sql_replacements(
    definition,
    target_endpoint,
):

    """
    Identify stale Sql.Database(
        server,
        database
    ) references.
    """

    replacements = {}

    sql_pattern = re.compile(
        r'Sql\.Database\('
        r'\s*"([^"]+)"'
        r'\s*,\s*'
        r'"([^"]+)"',
        re.IGNORECASE,
    )

    for _, content in get_tmdl_parts(
        definition
    ):

        for match in sql_pattern.finditer(
            content
        ):

            current_server = (
                match.group(1)
            )

            current_database = (
                match.group(2)
            )

            if (
                current_server
                != target_endpoint["server"]
            ):

                replacements[
                    current_server
                ] = target_endpoint["server"]

            if (
                current_database
                != target_endpoint["database"]
            ):

                replacements[
                    current_database
                ] = target_endpoint["database"]

    return replacements


def apply_replacements(
    definition,
    replacements,
):

    """
    Apply endpoint replacements across
    all TMDL parts.
    """

    updated_paths = []

    for part, content in get_tmdl_parts(
        definition
    ):

        updated = content

        for (
            old_value,
            new_value,
        ) in replacements.items():

            updated = updated.replace(
                old_value,
                new_value,
            )

        if updated != content:

            encode_part(
                part,
                updated,
            )

            updated_paths.append(
                part.get(
                    "path",
                    "<unknown>"
                )
            )

    return updated_paths


# -------------------------------------------------------------------------
# Takeover
# -------------------------------------------------------------------------

def takeover_semantic_model(
    workspace_id,
    semantic_model_id,
    headers,
):

    url = (
        f"{POWERBI_API}/groups/"
        f"{workspace_id}/datasets/"
        f"{semantic_model_id}/"
        f"Default.TakeOver"
    )

    response = api_request(
        "POST",
        url,
        headers,
    )

    if response.status_code not in (
        200,
        201,
    ):

        raise RuntimeError(
            "Takeover failed "
            f"({response.status_code}): "
            f"{response.text}"
        )

    print(
        "  Semantic model ownership "
        "takeover completed."
    )


# -------------------------------------------------------------------------
# Refresh
# -------------------------------------------------------------------------

def get_refresh_history(
    workspace_id,
    semantic_model_id,
    headers,
):

    url = (
        f"{POWERBI_API}/groups/"
        f"{workspace_id}/datasets/"
        f"{semantic_model_id}/"
        f"refreshes?$top=10"
    )

    response = api_request(
        "GET",
        url,
        headers,
    )

    response.raise_for_status()

    return (
        response
        .json()
        .get("value", [])
    )


def parse_datetime(value):

    if not value:
        return None

    try:

        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        )

    except ValueError:

        return None


def find_new_refresh(
    refreshes,
    submitted_after,
):

    candidates = []

    for refresh in refreshes:

        start_time = parse_datetime(
            refresh.get(
                "startTime"
            )
        )

        if (
            start_time
            and start_time
            >= submitted_after
        ):

            candidates.append(
                (
                    start_time,
                    refresh,
                )
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return candidates[0][1]


def refresh_semantic_model(
    workspace_id,
    semantic_model_id,
    headers,
):

    """
    Trigger full refresh and monitor
    refresh history.
    """

    url = (
        f"{POWERBI_API}/groups/"
        f"{workspace_id}/datasets/"
        f"{semantic_model_id}/refreshes"
    )

    submitted_after = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
    )

    response = api_request(
        "POST",
        url,
        headers,
        json={
            "notifyOption":
            "NoNotification",

            "type":
            "Full",
        },
    )

    if response.status_code != 202:

        raise RuntimeError(
            "Refresh submission failed "
            f"({response.status_code}): "
            f"{response.text}"
        )

    print(
        "  Refresh submitted."
    )

    for attempt in range(
        1,
        REFRESH_MAX_ATTEMPTS + 1,
    ):

        time.sleep(
            REFRESH_POLL_SECONDS
        )

        refreshes = get_refresh_history(
            workspace_id,
            semantic_model_id,
            headers,
        )

        refresh = find_new_refresh(
            refreshes,
            submitted_after,
        )

        if not refresh:

            print(
                "  Waiting for refresh "
                f"({attempt}/"
                f"{REFRESH_MAX_ATTEMPTS})"
            )

            continue

        status = refresh.get(
            "status",
            "Unknown"
        )

        refresh_id = refresh.get(
            "requestId",
            "Unknown"
        )

        print(
            f"  Refresh {refresh_id}: "
            f"{status}"
        )

        if status == "Completed":

            print(
                "  Refresh completed "
                "successfully."
            )

            return True

        if status in (
            "Failed",
            "Cancelled",
            "Disabled",
        ):

            error = (
                refresh.get(
                    "serviceExceptionJson"
                )
                or
                refresh.get(
                    "messages"
                )
                or
                "No error details returned."
            )

            raise RuntimeError(
                f"Refresh {status}: "
                f"{error}"
            )

    raise TimeoutError(
        "Semantic model refresh "
        "did not complete."
    )


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Repair Import-mode semantic "
            "model Lakehouse connections."
        )
    )

    parser.add_argument(
        "--aztenantid",
        required=True,
    )

    parser.add_argument(
        "--azclientid",
        required=True,
    )

    parser.add_argument(
        "--azspsecret",
        required=True,
    )

    parser.add_argument(
        "--target_env",
        required=True,
    )

    parser.add_argument(
        "--target_lakehouse",
        default="DemoLakehouse",
    )

    parser.add_argument(
        "--semantic_model",
        help=(
            "Optional exact semantic "
            "model name."
        ),
    )

    parser.add_argument(
        "--allow_mixed_mode",
        action="store_true",
    )

    parser.add_argument(
        "--skip_takeover",
        action="store_true",
    )

    parser.add_argument(
        "--skip_refresh",
        action="store_true",
    )

    args = parser.parse_args()


    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    print(
        "\nAuthenticating..."
    )

    credential = ClientSecretCredential(
        tenant_id=args.aztenantid,
        client_id=args.azclientid,
        client_secret=args.azspsecret,
    )

    token_manager = TokenManager(
        credential
    )

    fabric_headers = (
        token_manager
        .get_fabric_headers()
    )


    # ------------------------------------------------------------------
    # Workspace
    # ------------------------------------------------------------------

    env_variable = (
        f"{args.target_env}"
        f"WorkspaceName"
    ).upper()

    workspace_name = os.getenv(
        env_variable
    )

    if not workspace_name:

        raise ValueError(
            f"Environment variable "
            f"'{env_variable}' not found."
        )

    workspace_id = get_workspace_id(
        workspace_name,
        fabric_headers,
    )

    print(
        f"Workspace: {workspace_name}"
    )

    print(
        f"Workspace ID: {workspace_id}"
    )


    # ------------------------------------------------------------------
    # Lakehouse
    # ------------------------------------------------------------------

    lakehouses = list_items(
        workspace_id,
        "Lakehouse",
        fabric_headers,
    )

    target_lakehouse = next(
        (
            lakehouse
            for lakehouse
            in lakehouses
            if (
                lakehouse.get(
                    "displayName"
                )
                == args.target_lakehouse
            )
        ),
        None,
    )

    if not target_lakehouse:

        raise ValueError(
            f"Lakehouse "
            f"'{args.target_lakehouse}' "
            f"not found."
        )

    print(
        "\nGetting SQL analytics "
        f"endpoint for "
        f"'{args.target_lakehouse}'..."
    )

    target_endpoint = (
        get_lakehouse_sql_endpoint(
            workspace_id,
            target_lakehouse["id"],
            fabric_headers,
        )
    )

    print(
        "Target SQL server:"
    )

    print(
        f"  {target_endpoint['server']}"
    )

    print(
        "Target SQL database:"
    )

    print(
        f"  {target_endpoint['database']}"
    )


    # ------------------------------------------------------------------
    # Semantic Models
    # ------------------------------------------------------------------

    semantic_models = list_items(
        workspace_id,
        "SemanticModel",
        fabric_headers,
    )

    warehouses = list_items(
        workspace_id,
        "Warehouse",
        fabric_headers,
    )

    default_names = {
        item.get("displayName")
        for item
        in lakehouses + warehouses
        if item.get("displayName")
    }


    # Optional single model
    if args.semantic_model:

        semantic_models = [
            sm
            for sm in semantic_models
            if (
                sm.get("displayName")
                == args.semantic_model
            )
        ]

        if not semantic_models:

            raise ValueError(
                "Semantic model "
                f"'{args.semantic_model}' "
                "not found."
            )


    failed = []
    repaired = []
    skipped = []


    # ------------------------------------------------------------------
    # Process Models
    # ------------------------------------------------------------------

    for sm in semantic_models:

        model_name = sm.get(
            "displayName",
            "<unnamed>"
        )

        model_id = sm["id"]


        # Skip automatically generated models
        if model_name in default_names:

            print(
                "\nSkipping default model: "
                f"{model_name}"
            )

            skipped.append(
                model_name
            )

            continue


        print(
            "\n================================"
        )

        print(
            f"Processing: {model_name}"
        )

        print(
            "================================"
        )


        try:

            fabric_headers = (
                token_manager
                .get_fabric_headers()
            )

            powerbi_headers = (
                token_manager
                .get_powerbi_headers()
            )


            # ----------------------------------------------------------
            # Definition
            # ----------------------------------------------------------

            print(
                "  Reading semantic "
                "model definition..."
            )

            definition = (
                get_semantic_model_definition(
                    workspace_id,
                    model_id,
                    fabric_headers,
                )
            )


            # ----------------------------------------------------------
            # STORAGE MODE CHECK
            # ----------------------------------------------------------

            storage_modes = (
                detect_storage_modes(
                    definition
                )
            )

            process, reason = (
                should_process_model(
                    storage_modes,
                    args.allow_mixed_mode,
                )
            )

            print(
                f"  Storage mode: {reason}"
            )


            # Critical behaviour:
            # Do NOT repair or refresh
            # Direct Lake / DirectQuery models.

            if not process:

                print(
                    "  Skipping connection "
                    "repair."
                )

                skipped.append(
                    model_name
                )

                continue


            # ----------------------------------------------------------
            # CONNECTION REPAIR
            # ----------------------------------------------------------

            print(
                "  Checking SQL endpoint "
                "references..."
            )

            replacements = (
                build_sql_replacements(
                    definition,
                    target_endpoint,
                )
            )


            if replacements:

                print(
                    "  Connection changes "
                    "required:"
                )

                for (
                    old_value,
                    new_value,
                ) in replacements.items():

                    print(
                        f"    {old_value}"
                    )

                    print(
                        f"      -> {new_value}"
                    )


                updated_paths = (
                    apply_replacements(
                        definition,
                        replacements,
                    )
                )


                if not updated_paths:

                    raise RuntimeError(
                        "Replacement values "
                        "were found but no "
                        "TMDL file changed."
                    )


                print(
                    "  Updated TMDL parts:"
                )

                for path in updated_paths:

                    print(
                        f"    {path}"
                    )


                # ------------------------------------------------------
                # Publish corrected definition
                # ------------------------------------------------------

                print(
                    "  Updating semantic "
                    "model definition..."
                )

                update_semantic_model_definition(
                    workspace_id,
                    model_id,
                    definition,
                    fabric_headers,
                )

                print(
                    "  Definition updated."
                )


            else:

                print(
                    "  SQL connection "
                    "already points to "
                    "target Lakehouse."
                )


            # ----------------------------------------------------------
            # TAKEOVER
            # ----------------------------------------------------------

            if not args.skip_takeover:

                print(
                    "  Taking ownership..."
                )

                takeover_semantic_model(
                    workspace_id,
                    model_id,
                    powerbi_headers,
                )


            # ----------------------------------------------------------
            # IMPORT REFRESH
            # ----------------------------------------------------------

            if not args.skip_refresh:

                print(
                    "  Starting Import "
                    "refresh..."
                )

                refresh_semantic_model(
                    workspace_id,
                    model_id,
                    powerbi_headers,
                )


            repaired.append(
                model_name
            )


        except Exception as error:

            print(
                f"  FAILED: {error}"
            )

            failed.append(
                model_name
            )


    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    print(
        "\n================================"
    )

    print(
        "POST-DEPLOYMENT SUMMARY"
    )

    print(
        "================================"
    )

    print(
        f"Processed successfully: "
        f"{len(repaired)}"
    )

    print(
        f"Skipped: "
        f"{len(skipped)}"
    )

    print(
        f"Failed: "
        f"{len(failed)}"
    )


    if repaired:

        print(
            "\nProcessed:"
        )

        for model in repaired:

            print(
                f"  + {model}"
            )


    if skipped:

        print(
            "\nSkipped:"
        )

        for model in skipped:

            print(
                f"  - {model}"
            )


    if failed:

        print(
            "\nFailed:"
        )

        for model in failed:

            print(
                f"  ! {model}"
            )

        sys.exit(1)


    print(
        "\nSemantic model "
        "post-deployment processing "
        "completed successfully."
    )


# -------------------------------------------------------------------------
# Entry Point
# -------------------------------------------------------------------------

if __name__ == "__main__":
    main()
