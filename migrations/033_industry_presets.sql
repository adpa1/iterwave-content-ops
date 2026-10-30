-- 033 — Tenant-editable industry presets, initially seeded from built-in definitions.
CREATE TABLE IF NOT EXISTS industry_presets (
    key                    TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    description            TEXT NOT NULL DEFAULT '',
    positioning            TEXT NOT NULL,
    persona_keywords_json  TEXT NOT NULL,
    forbidden_directions_json TEXT NOT NULL,
    extra_redlines_json    TEXT NOT NULL,
    dimensions_json        TEXT NOT NULL,
    platform_notes_json    TEXT NOT NULL,
    is_builtin             INTEGER NOT NULL DEFAULT 1 CHECK (is_builtin IN (0,1)),
    version                INTEGER NOT NULL DEFAULT 1,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_industry_presets_builtin
    ON industry_presets(is_builtin, name);
