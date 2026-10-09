# Upstream patches

None. The controller wraps the pinned Native Task API and does not patch or
write upstream source/database. OAuth/identity routing remains a setup/design
gate, not a reason to disable upstream authentication or callback validation.
