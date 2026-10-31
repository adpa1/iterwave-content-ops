-- Preserve which saved structure template shaped each generated content item.
ALTER TABLE contents ADD COLUMN template_id INTEGER REFERENCES content_assets(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_contents_template_id ON contents(template_id);
