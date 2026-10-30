-- Link a saved activation sample to its formal production content without deleting the sample.
ALTER TABLE sample_contents ADD COLUMN promoted_content_id TEXT REFERENCES contents(id);
ALTER TABLE sample_contents ADD COLUMN promoted_at DATETIME;
CREATE UNIQUE INDEX IF NOT EXISTS ux_sample_contents_promoted_content
    ON sample_contents(promoted_content_id) WHERE promoted_content_id IS NOT NULL;
