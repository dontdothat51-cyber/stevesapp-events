# Steve's App -- live events pack builder

Reads public ESPN scoreboards every 10 minutes and publishes a small gzip JSON ("what is on right now, and on which
network") to private storage for the Steve's App Fire TV player. Devices never call ESPN; only this builder does.

No customer data, no credentials and no app code live here. Two encrypted GitHub Actions secrets: a single-purpose
publish key (it can publish a pack and nothing else) and one provider login used only to read channel names. This repository is a mirror: the builder is maintained in the private app repository and
synced here with `tools/export_events_repo.py`.
