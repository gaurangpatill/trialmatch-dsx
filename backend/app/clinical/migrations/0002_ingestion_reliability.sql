ALTER TABLE ingestion_runs
    ADD COLUMN IF NOT EXISTS index_retrieved_count INTEGER NOT NULL DEFAULT 0 CHECK (index_retrieved_count >= 0),
    ADD COLUMN IF NOT EXISTS detail_cache_hit_count INTEGER NOT NULL DEFAULT 0 CHECK (detail_cache_hit_count >= 0),
    ADD COLUMN IF NOT EXISTS detail_network_count INTEGER NOT NULL DEFAULT 0 CHECK (detail_network_count >= 0),
    ADD COLUMN IF NOT EXISTS detail_resumed_count INTEGER NOT NULL DEFAULT 0 CHECK (detail_resumed_count >= 0),
    ADD COLUMN IF NOT EXISTS detail_failed_count INTEGER NOT NULL DEFAULT 0 CHECK (detail_failed_count >= 0),
    ADD COLUMN IF NOT EXISTS incomplete_detail_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (incomplete_detail_ids_json IS JSON ARRAY),
    ADD COLUMN IF NOT EXISTS ma_verified_count INTEGER NOT NULL DEFAULT 0 CHECK (ma_verified_count >= 0),
    ADD COLUMN IF NOT EXISTS ma_unverified_count INTEGER NOT NULL DEFAULT 0 CHECK (ma_unverified_count >= 0),
    ADD COLUMN IF NOT EXISTS ma_unverified_records_json TEXT NOT NULL DEFAULT '[]' CHECK (ma_unverified_records_json IS JSON ARRAY),
    ADD COLUMN IF NOT EXISTS request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    ADD COLUMN IF NOT EXISTS rate_limited_count INTEGER NOT NULL DEFAULT 0 CHECK (rate_limited_count >= 0),
    ADD COLUMN IF NOT EXISTS cohort_fingerprint TEXT CHECK (cohort_fingerprint IS NULL OR cohort_fingerprint ~ '^[a-f0-9]{64}$'),
    ADD COLUMN IF NOT EXISTS checkpoint_resumed INTEGER NOT NULL DEFAULT 0 CHECK (checkpoint_resumed IN (0, 1)),
    ADD COLUMN IF NOT EXISTS publication_attempted INTEGER NOT NULL DEFAULT 0 CHECK (publication_attempted IN (0, 1)),
    ADD COLUMN IF NOT EXISTS publication_completed INTEGER NOT NULL DEFAULT 0 CHECK (publication_completed IN (0, 1)),
    ADD COLUMN IF NOT EXISTS published_record_count INTEGER NOT NULL DEFAULT 0 CHECK (published_record_count >= 0),
    ADD COLUMN IF NOT EXISTS previous_snapshot_preserved INTEGER NOT NULL DEFAULT 0 CHECK (previous_snapshot_preserved IN (0, 1));

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ingestion_publication_consistent'
          AND conrelid = 'ingestion_runs'::regclass
    ) THEN
        ALTER TABLE ingestion_runs
            ADD CONSTRAINT ingestion_publication_consistent CHECK (
                (publication_completed = 0 AND published_record_count = 0)
                OR
                (publication_completed = 1 AND status = 'complete'
                 AND published_record_count = accepted_record_count)
            );
    END IF;
END
$$;
