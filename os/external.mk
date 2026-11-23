include $(sort $(wildcard $(BR2_EXTERNAL_BEINGOS_PATH)/package/*/*.mk))

# The memory's search uses SQLite's FTS5, which Buildroot leaves off.
SQLITE_CONF_OPTS += --fts5
