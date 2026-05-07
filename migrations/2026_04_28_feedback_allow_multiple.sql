-- Allow users to leave more than one feedback.
-- The original model had `unique=True, index=True` on user_id, which SQLAlchemy
-- materialised as a UNIQUE INDEX named `ix_feedbacks_user_id` (not a UNIQUE
-- constraint). We drop that index and recreate it as a non-unique one so
-- per-user lookups stay fast.

DO $$
BEGIN
    -- Drop the unique index if present
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'ix_feedbacks_user_id'
    ) THEN
        EXECUTE 'DROP INDEX public.ix_feedbacks_user_id';
        RAISE NOTICE 'Dropped index ix_feedbacks_user_id';
    END IF;

    -- Drop a possible unique constraint variant just in case
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'feedbacks' AND c.contype = 'u'
          AND pg_get_constraintdef(c.oid) LIKE '%(user_id)'
    ) THEN
        EXECUTE (
            SELECT format('ALTER TABLE feedbacks DROP CONSTRAINT %I', c.conname)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = 'feedbacks' AND c.contype = 'u'
              AND pg_get_constraintdef(c.oid) LIKE '%(user_id)'
            LIMIT 1
        );
        RAISE NOTICE 'Dropped unique constraint on feedbacks.user_id';
    END IF;

    -- Recreate the index as non-unique
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'ix_feedbacks_user_id'
    ) THEN
        EXECUTE 'CREATE INDEX ix_feedbacks_user_id ON feedbacks (user_id)';
        RAISE NOTICE 'Recreated ix_feedbacks_user_id as non-unique';
    END IF;
END $$;
