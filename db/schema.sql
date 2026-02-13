-- Run this in Supabase SQL Editor
CREATE TABLE IF NOT EXISTS clones (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    url TEXT NOT NULL,
    preview_url TEXT,
    sandbox_id TEXT,
    html TEXT,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'success', 'failed')),
    error_message TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Index for listing clones by recency
CREATE INDEX IF NOT EXISTS idx_clones_created_at ON clones (created_at DESC);

-- Index for looking up by URL
CREATE INDEX IF NOT EXISTS idx_clones_url ON clones (url);

-- ── Migration: add is_active and output_format columns ──────────────────
ALTER TABLE clones ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true;
ALTER TABLE clones ADD COLUMN IF NOT EXISTS output_format TEXT DEFAULT 'html';

-- Index for filtering active clones
CREATE INDEX IF NOT EXISTS idx_clones_is_active ON clones (is_active) WHERE is_active = true;
