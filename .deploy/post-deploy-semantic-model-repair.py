"""
Post-deployment semantic model connection repair script.
Adapted from FabricDevCamp/fabric-devops pattern.
Runs on the Azure DevOps agent (not as a Fabric notebook) to avoid XMLA auth issues.
Uses Fabric REST API and Power BI REST API with direct SP authentication.
"""

import os, sys, time, base64, re, argparse, json, requests
from azure.identity import ClientSecretCredential

FABRIC_API = "https://api.fabric.microsoft.com/v1"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"


class TokenManager:
    """Manages token acquisition for Fabric and Power BI APIs."""

    def __init__(self, credential):
        self.credential = credential

    def get_fabric_headers(self):
        token = self.credential.get_token("https://api.fabric.microsoft.com/.default")
        return {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}

    def get_powerbi_headers(self):
        token = self.credential.get_token("https://analysis.windows.net/powerbi/api/.default")
        return {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}


def get_workspace_id(workspace_name, headers):
    """Resolve workspace GUID from display name."""
    response = requests.get(f"{FABRIC_API}/workspaces", headers=headers)
    response.raise_for_status()
    for ws in response.json()["value"]:
        if ws["displayName"] == workspace_name:
            return ws["id"]
    raise ValueError(f"Workspace '{workspace_name}' not found.")


def list_items(workspace_id, item_type, headers):
    """List items of a given type in a workspace."""
    response = requests.get(f"{FABRIC_API}/workspaces/{workspace_id}/items?type={item_type}", headers=headers)
    response.raise_for_status()
    return response.json().get("value", [])


def get_lakehouse_sql_endpoint(workspace_id, lakehouse_id, headers):
    """Get the SQL endpoint for a lakehouse, polling until provisioned."""
    url = f"{FABRIC_API}/workspaces/{workspace_id}/lakehouses/{lakehouse_id}"
    for _ in range(30):
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        sql_props = response.json().get("properties", {}).get("sqlEndpointProperties", {})
        if sql_props.get("provisioningStatus") == "Success":
            return {"server": sql_props["connectionString"], "database": sql_props["id"]}
        print(f"  SQL endpoint provisioning: {sql_props.get('provisioningStatus')}, waiting...")
        time.sleep(10)
    raise TimeoutError("SQL endpoint did not provision in time.")


def poll_lro(location, headers, max_attempts=30):
    """Poll a Fabric long-running operation until completion. Returns the result URL."""
    retry_after = 5
    for _ in range(max_attempts):
        time.sleep(retry_after)
        response = requests.get(location, headers=headers)
        if response.status_code != 200:
            continue
        result = response.json()
        retry_after = int(response.headers.get("Retry-After", retry_after))
        if result.get("status") == "Succeeded":
            return location.rstrip("/") + "/result"
        if result.get("status") == "Failed":
            raise RuntimeError(f"LRO failed: {json.dumps(result.get('error', {}))}")
    raise TimeoutError("LRO polling timed out.")


def get_item_definition(workspace_id, item_id, headers):
    """Get item definition via Fabric REST API (handles LRO)."""
    url = f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/getDefinition"
    response = requests.post(url, headers=headers)

    if response.status_code == 202:
        result_url = poll_lro(response.headers["Location"], headers)
        response = requests.get(result_url, headers=headers)

    response.raise_for_status()
    result = response.json()
    if "definition" in result:
        return result
    if "parts" in result:
        return {"definition": result}
    return result


def update_item_definition(workspace_id, item_id, definition, headers):
    """Update item definition via Fabric REST API (handles LRO)."""
    url = f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/updateDefinition"
    response = requests.post(url, headers=headers, json={"definition": definition})
    if response.status_code == 202:
        poll_lro(response.headers.get("Location", ""), headers)
    elif response.status_code not in (200, 202):
        print(f"  Update definition failed ({response.status_code}): {response.text}")
        return False
    return True


def find_expressions_part(definition):
    """Find the expressions TMDL part and return (path, decoded_content)."""
    for part in definition.get("parts", []):
        if "expressions" in part["path"].lower():
            content = base64.b64decode(part["payload"]).decode("utf-8")
            return part["path"], content
    return None, None


def build_replacements(tmdl_content, target_endpoint):
    """Scan TMDL content for stale Sql.Database references and build replacements."""
    replacements = {}
    for server in re.findall(r'Sql\.Database\("([^"]+)"', tmdl_content):
        if server != target_endpoint["server"]:
            replacements[server] = target_endpoint["server"]
    for database in re.findall(r'Sql\.Database\("[^"]+",\s*"([^"]+)"', tmdl_content):
        if database != target_endpoint["database"]:
            replacements[database] = target_endpoint["database"]
    return replacements


def apply_replacements(definition, part_path, replacements):
    """Apply search-and-replace to a base64-encoded definition part."""
    for part in definition.get("parts", []):
        if part["path"] == part_path:
            payload = base64.b64decode(part["payload"]).decode("utf-8")
            for old_val, new_val in replacements.items():
                payload = payload.replace(old_val, new_val)
            part["payload"] = base64.b64encode(payload.encode("utf-8")).decode("utf-8")
            return True
    return False


def find_or_create_connection(server, database, ws_id, lh_name, tenant_id, client_id, client_secret, headers):
    """Find an existing SQL connection or create a new one."""
    display_name = f"Workspace[{ws_id}]-Lakehouse[{lh_name}]-SqlEndpoint"

    # Search existing connections
    response = requests.get(f"{FABRIC_API}/connections", headers=headers)
    if response.status_code == 200:
        for conn in response.json().get("value", []):
            if conn.get("displayName") == display_name:
                print(f"  Reusing existing connection: {conn['id']}")
                return conn["id"]

    # Create new connection
    body = {
        "displayName": display_name,
        "connectivityType": "ShareableCloud",
        "privacyLevel": "Organizational",
        "connectionDetails": {
            "type": "SQL", "creationMethod": "Sql",
            "parameters": [
                {"value": server, "dataType": "Text", "name": "server"},
                {"value": database, "dataType": "Text", "name": "database"}
            ]
        },
        "credentialDetails": {
            "credentials": {
                "tenantId": tenant_id, "servicePrincipalClientId": client_id,
                "servicePrincipalSecret": client_secret, "credentialType": "ServicePrincipal"
            },
            "singleSignOnType": "None", "connectionEncryption": "NotEncrypted",
            "skipTestConnection": "false"
        }
    }
    response = requests.post(f"{FABRIC_API}/connections", headers=headers, json=body)
    if response.status_code in (200, 201):
        conn_id = response.json()["id"]
        print(f"  Created new connection: {conn_id}")
        return conn_id
    print(f"  Failed to create connection ({response.status_code}): {response.text}")
    return None


def takeover_semantic_model(ws_id, sm_id, headers):
    """Take over ownership of a semantic model."""
    response = requests.post(f"{POWERBI_API}/groups/{ws_id}/datasets/{sm_id}/Default.TakeOver", headers=headers)
    if response.status_code == 200:
        print(f"  Took over ownership")
    else:
        print(f"  Takeover failed ({response.status_code}): {response.text}")


def bind_semantic_model(ws_id, sm_id, conn_id, headers):
    """Bind semantic model to a SQL connection."""
    body = {"gatewayObjectId": "00000000-0000-0000-0000-000000000000", "datasourceObjectIds": [conn_id]}
    response = requests.post(f"{POWERBI_API}/groups/{ws_id}/datasets/{sm_id}/Default.BindToGateway", headers=headers, json=body)
    if response.status_code == 200:
        print(f"  Bound to connection")
        return True
    print(f"  Bind failed ({response.status_code}): {response.text}")
    return False


def refresh_semantic_model(ws_id, sm_id, headers):
    """Trigger a full refresh and wait for completion."""
    response = requests.post(
        f"{POWERBI_API}/groups/{ws_id}/datasets/{sm_id}/refreshes",
        headers=headers, json={"notifyOption": "NoNotification", "type": "Full"}
    )
    if response.status_code != 202:
        print(f"  Refresh trigger failed ({response.status_code}): {response.text}")
        return
    refresh_id = response.headers.get("x-ms-request-id")
    if not refresh_id:
        print("  Refresh triggered (no polling ID)")
        return
    poll_url = f"{POWERBI_API}/groups/{ws_id}/datasets/{sm_id}/refreshes/{refresh_id}"
    for _ in range(120):
        time.sleep(6)
        r = requests.get(poll_url, headers=headers)
        if r.status_code == 200:
            details = r.json()
            status = details.get("status", "Unknown")
            if status not in ("Unknown", "NotStarted", "InProgress"):
                print(f"  Refresh status: {status}")
                if status == "Failed":
                    print(f"  Error: {details.get('serviceExceptionJson', 'N/A')}")
                return


def main():
    parser = argparse.ArgumentParser(description="Post-deployment semantic model connection repair.")
    parser.add_argument("--aztenantid", required=True)
    parser.add_argument("--azclientid", required=True)
    parser.add_argument("--azspsecret", required=True)
    parser.add_argument("--target_env", required=True)
    parser.add_argument("--target_lakehouse", default="DemoLakehouse")
    args = parser.parse_args()

    # Authenticate
    print("Authenticating...")
    credential = ClientSecretCredential(client_id=args.azclientid, client_secret=args.azspsecret, tenant_id=args.aztenantid)
    token_mgr = TokenManager(credential)
    fabric_headers = token_mgr.get_fabric_headers()

    # Resolve workspace
    ws_name = os.environ[f"{args.target_env}WorkspaceName".upper()]
    print(f"Workspace: {ws_name}")
    ws_id = get_workspace_id(ws_name, fabric_headers)

    # Get target lakehouse SQL endpoint
    print(f"Getting SQL endpoint for '{args.target_lakehouse}'...")
    lakehouses = list_items(ws_id, "Lakehouse", fabric_headers)
    target_lh = next((lh for lh in lakehouses if lh["displayName"] == args.target_lakehouse), None)
    if not target_lh:
        raise ValueError(f"Lakehouse '{args.target_lakehouse}' not found.")
    new_endpoint = get_lakehouse_sql_endpoint(ws_id, target_lh["id"], fabric_headers)
    print(f"Target: server={new_endpoint['server']}, database={new_endpoint['database']}")

    # Identify non-default semantic models
    all_sms = list_items(ws_id, "SemanticModel", fabric_headers)
    default_names = {i["displayName"] for i in lakehouses + list_items(ws_id, "Warehouse", fabric_headers)}
    failed = False

    for sm in all_sms:
        if sm["displayName"] in default_names:
            continue

        print(f"\nProcessing: {sm['displayName']}")
        try:
            fabric_headers = token_mgr.get_fabric_headers()
            pbi_headers = token_mgr.get_powerbi_headers()

            # Get definition and find expressions part
            defn = get_item_definition(ws_id, sm["id"], fabric_headers)
            definition = defn.get("definition", {})
            expr_path, tmdl_content = find_expressions_part(definition)

            if not expr_path:
                print("  No expressions part found, skipping")
                continue

            # Build and apply replacements
            replacements = build_replacements(tmdl_content, new_endpoint)
            if replacements:
                print(f"  Replacing: {list(replacements.keys())}")
                apply_replacements(definition, expr_path, replacements)
                if not update_item_definition(ws_id, sm["id"], definition, fabric_headers):
                    failed = True
                    continue
                print("  Definition updated")
            else:
                print("  Definition already correct")

            # Create/find connection, takeover, bind
            conn_id = find_or_create_connection(
                new_endpoint["server"], new_endpoint["database"],
                ws_id, args.target_lakehouse,
                args.aztenantid, args.azclientid, args.azspsecret, fabric_headers
            )
            if not conn_id:
                failed = True
                continue

            takeover_semantic_model(ws_id, sm["id"], pbi_headers)
            if not bind_semantic_model(ws_id, sm["id"], conn_id, pbi_headers):
                failed = True
                continue

            # Refresh
            print("  Refreshing...")
            refresh_semantic_model(ws_id, sm["id"], pbi_headers)

        except Exception as e:
            print(f"  FAILED: {e}")
            failed = True

    if failed:
        print("\nSome semantic models failed. Check logs above.")
        sys.exit(1)
    else:
        print("\nAll semantic models repaired successfully.")


if __name__ == "__main__":
    main()
