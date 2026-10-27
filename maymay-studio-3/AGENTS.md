# MayMay Studio

Read README.md and docs/ARCHITECTURE.md before changes. Keep provider billing actions explicit and never automatically retry an uncertain paid create. Do not add synthetic benchmark values or claim GPU validation based on CPU tests. Keep GPU dependencies out of the web server. Preserve job attempt fencing, private object storage, and dataset group splits. Run `python -m pytest -q` and `npm --prefix frontend run build` for affected changes. Recipe changes need primary-source review and a real GPU qualification before recommendation.
