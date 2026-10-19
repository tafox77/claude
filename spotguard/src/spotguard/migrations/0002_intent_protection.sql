-- Stop/target travel with the entry intent so protection is applied atomically with the first fill,
-- whichever path delivers that fill (immediate ack, later reconciliation, crash recovery).
ALTER TABLE intents ADD COLUMN stop TEXT;
ALTER TABLE intents ADD COLUMN target TEXT
