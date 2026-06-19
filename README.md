## Harvest Proknow

Queries Proknow, collects data and writes to the BigDB (psql@192.168.117:32899). This script should be run fairly regularly to make sure the DB stays up-to-date.

Used alongside EDNA as part of the Continuous Improvement platform.

# Design and link to EDNA
![System design](./static/Donal-Diagram.svg)

# TODO
    - Don't rely on patient IDs. Query ProKnow daily to detect new patients in WORKSPACE.
    - Querying CWP only works on Windows (user authentication).
    - CWP Query only returns latest booking form. Might be conflicts for re-treated patients.

