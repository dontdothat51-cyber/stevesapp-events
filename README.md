# Steve's App -- live events pack builder

Every 5 minutes: reads public ESPN scoreboards into a small gzip JSON ("what is on right now, and on which network")
and mirrors a provider's event-channel names into another, both handed to the Steve's App server for its Fire TV
player. Devices never call ESPN and never poll the provider for names; only this builder does.

No customer data, no credentials and no app code live here. Two encrypted GitHub Actions secrets: a single-purpose
publish key (it can publish a pack and nothing else) and one provider login used only to read channel names. This repository is a mirror: the builder is maintained in the private app repository and
synced here with `tools/export_events_repo.py`.
