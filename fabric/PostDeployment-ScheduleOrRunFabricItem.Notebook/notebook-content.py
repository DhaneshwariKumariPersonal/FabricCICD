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
from azure.keyvault.secrets import SecretClient

# Replace with your Key Vault name
key_vault_name = "kv-fabriccicddemo" 
kv_uri = f"https://{key_vault_name}.vault.azure.net/"

# Retrieve the Service Principal credentials
client_id=notebookutils.credentials.getSecret(kv_uri,"azclientid")
tenant_id=notebookutils.credentials.getSecret(kv_uri,"aztenantid")
client_secret=notebookutils.credentials.getSecret(kv_uri,"azspsecret")

# Authenticate using SPN
credential = ClientSecretCredential(tenant_id, client_id, client_secret)
token = credential.get_token("https://api.fabric.microsoft.com/.default").token


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# This is Post Deployment Activity to Run On-Demand Notebook via Fabric Rest API ensure to pass correct workspaceid and notebookid

# Replace with your actual values. This workspace id and notebook id is Actual Notebook available
workspace_id = '57bb6599-d2f2-45eb-8d5f-52a1348e1aea'
notebook_id = 'd7d20470-8d8e-4d4f-bf33-d87ae41c1469' 



url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{notebook_id}/jobs/instances?jobType=RunNotebook"

# Set headers
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

# Include an empty JSON payload to satisfy API requirements
response = requests.post(url, headers=headers, json={})

# Handle response
if response.status_code == 202:
    print("Notebook execution started successfully.")
else:
    print(f"Failed to start notebook: {response.status_code} - {response.text}")


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# This is Post Deployment Activity to Run On-Demand Notebook via Fabric Rest API ensure to pass correct workspaceid and datapipeline_id

# Replace with your actual values. This workspace id and DataPipeline id is Actual DataPipeline available
#workspace_id = '57bb6599-d2f2-45eb-8d5f-52a1348e1aea'  # Feature Env
#datapipeline_id = 'db2989cc-e275-402e-9a29-631f742e5bef' # Feature Env

#workspace_id = '6dc052a6-0366-4444-9bd1-506dc2833bc9' # Dev Env
#datapipeline_id = 'b6ed2d19-5fb9-4086-87de-73b39c80fb55' # Dev Env

workspace_id = '1c355cdf-d807-4904-9de5-8ecd7dea2c95' # Test Env
datapipeline_id = '65ab6d0e-359e-4c2c-aba1-45e097ba3b84' # Test Env

url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{datapipeline_id}/jobs/instances?jobType=Pipeline"

# Set headers
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}


payload = {
    "executionData": {
        "parameters": {
            "src_workspace": "HMLR-test",
            "src_lakehouse": "DemoLakehouse",
            "src_table": "countries",
            "tgt_workspace": "HMLR-test",
            "tgt_lakehouse": "DemoLakehouse-shortcut",
            "tgt_table": "countries"
        }
    }
}


# Include an empty JSON payload to satisfy API requirements
response = requests.post(url, headers=headers, json=payload)

# Handle response
if response.status_code == 202:
    print("Data Pipeline execution started successfully.")
else:
    print(f"Failed to start Data Pipeline: {response.status_code} - {response.text}")


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# This is Post Deployment Activity to Schedule Notebook via Fabric Rest API ensure to pass correct workspaceid and notebookid
#workspace_id='57bb6599-d2f2-45eb-8d5f-52a1348e1aea'
#notebook_id='d7d20470-8d8e-4d4f-bf33-d87ae41c1469'
#job_type='RunNotebook'

workspace_id = '1c355cdf-d807-4904-9de5-8ecd7dea2c95' # Test Env
datapipeline_id = '65ab6d0e-359e-4c2c-aba1-45e097ba3b84' # Test Env
job_type="Pipeline"

# Schedule a Fabric Notebook via REST API endpoint
url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{datapipeline_id}/jobs/{job_type}/schedules"

# Headers
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

# Schedule payload
payload = {  
  "executionData": {
        "parameters": {
            "src_workspace": "HMLR-test",
            "src_lakehouse": "DemoLakehouse",
            "src_table": "countries",
            "tgt_workspace": "HMLR-test",
            "tgt_lakehouse": "DemoLakehouse-shortcut",
            "tgt_table": "countries"
        }
    },
  "enabled": True,
  "configuration": {
    "startDateTime": "2025-10-01T00:00:00",
    "endDateTime": "2025-12-31T23:59:00",
    "localTimeZoneId": "Central Standard Time",
    "type": "Cron",
    "interval": 1
  }
}


# Create schedule
response = requests.post(url, headers=headers, json=payload)

# Handle response
if response.status_code == 201:
    print("DataPipeline Job created successfully.")
else:
    print(f"Failed to start DataPipeline: {response.status_code} - {response.text}")


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
