# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {}
# META }

# PARAMETERS CELL ********************

# MAGIC %%configure
# MAGIC {
# MAGIC   "defaultLakehouse": 
# MAGIC   {
# MAGIC     "name": { "parameterName": "TargetLakehouseName","defaultValue": "DemoLakehouse" },
# MAGIC     "id": { "parameterName": "TargetLakehouseId", "defaultValue": "1a7672ef-59c2-4e15-b475-451bf476f599" }, 
# MAGIC     "workspaceId": { "parameterName": "TargetWorkspaceId","defaultValue": "b373f55a-2989-4443-9209-6353e0636a5c" } 
# MAGIC   }
# MAGIC }

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

import pandas as pd
from tqdm.auto import tqdm

base = "https://synapseaisolutionsa.z13.web.core.windows.net/data/AdventureWorks"

# load list of tables
df_tables = pd.read_csv(f"{base}/adventureworks.csv", names=["table"])

for table in (pbar := tqdm(df_tables["table"].values)):
    pbar.set_description(f"Uploading {table} to lakehouse")

    # download first, so a failed download doesn't leave you with a dropped table
    df = pd.read_parquet(f"{base}/{table}.parquet")

    # drop the table if it already exists
    spark.sql(f"DROP TABLE IF EXISTS `{table}`")

    # save as lakehouse table
    spark.createDataFrame(df).write.format("delta").mode("overwrite").saveAsTable(table)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
