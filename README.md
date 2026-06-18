## Harvest Proknow

Queries Proknow, collects data and writes to the BigDB (psql@192.168.117:32899). This script should be run fairly regularly to make sure the DB stays up-to-date.
Given a list of IDS, will fetch (1) proknow metadata, (2) scorecards? (3) custom metrics? (4) planning data to calculate locally.

E.g. of stats calculated locally:
    - Distance between specific OARs.

# TODO
    - Don't rely on patient IDs. 