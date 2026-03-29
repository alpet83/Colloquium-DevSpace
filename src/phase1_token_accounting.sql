-- Phase 1: Token Accounting Schema Extension
-- Add token tracking columns and pricing table to CQDS PostgreSQL

-- Step 1: Add token tracking columns to posts table if they don't exist
ALTER TABLE IF EXISTS posts ADD COLUMN IF NOT EXISTS output_tokens INTEGER DEFAULT 0;
ALTER TABLE IF EXISTS posts ADD COLUMN IF NOT EXISTS model_name TEXT DEFAULT 'unknown';
ALTER TABLE IF EXISTS posts ADD COLUMN IF NOT EXISTS input_tokens INTEGER DEFAULT 0;

-- Step 2: Create pricing lookup table
CREATE TABLE IF NOT EXISTS token_pricing (
    model_name TEXT PRIMARY KEY,
    input_cost_per_1k DECIMAL(10, 8) NOT NULL,
    output_cost_per_1k DECIMAL(10, 8) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Step 3: Insert default pricing for available LLM models
INSERT INTO token_pricing (model_name, input_cost_per_1k, output_cost_per_1k) VALUES
    ('gpt5c', 0.003, 0.006),        -- GPT-5 (example rates, adjust as needed)
    ('claude4s', 0.003, 0.015),     -- Claude 4 Sonnet  
    ('claude4o', 0.002, 0.010),     -- Claude 4 Opus
    ('grok4f', 0.001, 0.002),       -- Grok 4 Fast
    ('grok4c', 0.001, 0.002),       -- Grok 4 Complex  
    ('nemotron3s', 0.0005, 0.0010)  -- Nemotron 3
ON CONFLICT (model_name) DO NOTHING;

-- Step 4: Verify schema after changes
SELECT 'Token accounting schema extension complete' as status;
SELECT column_name, data_type FROM information_schema.columns 
WHERE table_name = 'posts' 
ORDER BY ordinal_position;

SELECT 'Pricing table rows:' as status;
SELECT * FROM token_pricing;
