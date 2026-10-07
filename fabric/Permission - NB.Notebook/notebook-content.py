# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {}
# META }

# CELL ********************

import requests
from azure.identity import ClientSecretCredential

# --- Key Vault / SPN credentials ---
key_vault_name = "kv-fabriccicddemo"
kv_uri = f"https://{key_vault_name}.vault.azure.net/"

client_id     = notebookutils.credentials.getSecret(kv_uri, "azclientid")
tenant_id     = notebookutils.credentials.getSecret(kv_uri, "aztenantid")
client_secret = notebookutils.credentials.getSecret(kv_uri, "azspsecret")

credential = ClientSecretCredential(tenant_id, client_id, client_secret)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Config ---
workspace_id = "348aa42c-052d-454d-b94a-9941ddfb9b63"
emails       = ["admin@MngEnvMCAP804890.onmicrosoft.com"]
role         = "Contributor"   # Admin | Member | Contributor | Viewer


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


# --- Tokens (separate audiences) ---
fabric_token = credential.get_token("https://api.fabric.microsoft.com/.default").token
graph_token  = credential.get_token("https://graph.microsoft.com/.default").token

fabric_hdr = {"Authorization": f"Bearer {fabric_token}", "Content-Type": "application/json"}
graph_hdr  = {"Authorization": f"Bearer {graph_token}", "ConsistencyLevel": "eventual"}

base = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/roleAssignments"


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Resolve email -> (object id, UPN) via Graph ---
def resolve_user(email):
    e = email.replace("'", "''")
    flt = (f"mail eq '{e}' or userPrincipalName eq '{e}' "
           f"or otherMails/any(m:m eq '{e}')")
    r = requests.get(
        "https://graph.microsoft.com/v1.0/users",
        headers=graph_hdr,
        params={"$filter": flt, "$select": "id,userPrincipalName,mail", "$count": "true"},
    )
    r.raise_for_status()
    users = r.json().get("value", [])
    if not users:
        raise ValueError(f"{email}: not found in tenant (is the guest invited?)")
    if len(users) > 1:
        raise ValueError(f"{email}: matched {len(users)} users, be more specific")
    return users[0]["id"], users[0]["userPrincipalName"]


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


# --- Assign role ---
results = []
for email in emails:
    try:
        oid, upn = resolve_user(email)
        body = {
            "principal": {
                "id": oid,
                "type": "User",
                "userDetails": {"userPrincipalName": upn}
            },
            "role": role
        }
        r = requests.post(base, headers=fabric_hdr, json=body)

        if r.status_code in (200, 201):
            status = "assigned"
        elif r.status_code == 409:
            u = requests.patch(f"{base}/{oid}", headers=fabric_hdr, json={"role": role})
            u.raise_for_status()
            status = "updated"
        else:
            r.raise_for_status()
        results.append((email, upn, oid, status))
    except Exception as ex:
        results.append((email, None, None, f"FAILED: {ex}"))

for row in results:
    print(row)

# --- Verify ---
print("\nCurrent assignments:")
for a in requests.get(base, headers=fabric_hdr).json().get("value", []):
    print(a["principal"].get("displayName"), a["principal"]["type"], a["role"])

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
